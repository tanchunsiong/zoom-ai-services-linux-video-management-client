from __future__ import annotations

import copy
import json
import math
import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

# GTK's default NGL renderer probes EGL/Zink even on virtual machines that only
# expose a display-only DRM device (for example bochs-drm). That probe produces
# several alarming warnings before falling back. Select the Cairo renderer only
# when Linux has no render node; real GPU-backed sessions keep GTK's default.
if (
    "GSK_RENDERER" not in os.environ
    and Path("/dev/dri").is_dir()
    and not any(Path("/dev/dri").glob("renderD*"))
):
    os.environ["GSK_RENDERER"] = "cairo"

import gi
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

from . import __version__, vtt
from .estimators import (
    cost_comparison, format_time, format_usd, learn_time_calibration,
    sum_breakdowns, time_comparison,
)
from .live import (
    DEFAULT_VOCABULARY, LiveCallbacks, LiveSession, audio_device_options,
    completed_transcript_display, completed_transcript_source,
    floating_caption_segments,
)
from .media import MEDIA_EXTENSIONS, basic_validation_error, discover, probe as media_probe, tool_available
from .models import Credentials, JobState, LANGUAGES, QueueJob, translation_route
from .pipeline import MediaPipeline, apply_existing
from .playback import PlaybackResolver
from .storage import AppPaths, Store
from .zoom import CancelledError, ZoomClient


APP_ID = "com.tanchunsiong.ZScribeLinux"


def clock(seconds: float | None) -> str:
    value = max(0, int(seconds or 0))
    if value >= 3600:
        return f"{value // 3600:02d}:{value // 60 % 60:02d}:{value % 60:02d}"
    return f"{value // 60:02d}:{value % 60:02d}"


class JobRow(Gtk.ListBoxRow):
    def __init__(self, job: QueueJob, settings, calibration):
        super().__init__()
        self.job_id = job.id
        self.set_activatable(True)
        root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        root.set_margin_top(10)
        root.set_margin_bottom(10)
        root.set_margin_start(12)
        root.set_margin_end(12)
        icon = Gtk.Image.new_from_icon_name(
            "audio-x-generic-symbolic" if Path(job.source_path).suffix.lower() in {".wav", ".m4a", ".mp3", ".aac", ".flac", ".ogg", ".opus", ".aiff"}
            else "video-x-generic-symbolic"
        )
        icon.set_pixel_size(28)
        root.append(icon)
        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        details.set_hexpand(True)
        name = Gtk.Label(label=job.display_name, xalign=0)
        name.set_ellipsize(3)
        name.add_css_class("heading")
        details.append(name)
        options = f"{LANGUAGES.get(job.source_language, job.source_language)}"
        if job.has_translation:
            options += f" → {LANGUAGES.get(job.translation_language, job.translation_language)}"
        if job.summarize:
            options += "  •  Summary"
        meta = Gtk.Label(label=f"{clock(job.duration_seconds)}  •  {options}", xalign=0)
        meta.add_css_class("dim-label")
        meta.add_css_class("caption")
        details.append(meta)
        root.append(details)
        status = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        status.set_size_request(230, -1)
        label = Gtk.Label(label=job.state.capitalize(), xalign=0)
        label.add_css_class("caption-heading")
        if job.state == JobState.READY.value:
            label.add_css_class("success")
        elif job.state == JobState.FAILED.value:
            label.add_css_class("error")
        status.append(label)
        if JobState(job.state).processing:
            progress = Gtk.ProgressBar()
            progress.set_fraction(job.progress)
            progress.set_tooltip_text(job.status_message)
            status.append(progress)
        else:
            message = Gtk.Label(label=job.error or job.status_message, xalign=0)
            message.set_ellipsize(3)
            message.set_max_width_chars(34)
            message.add_css_class("caption")
            message.add_css_class("dim-label")
            status.append(message)
        root.append(status)
        cost = cost_comparison(job, settings)
        timing = time_comparison(job, settings, calibration)
        metrics = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        metrics.set_size_request(205, -1)
        estimate = Gtk.Label(
            label=f"Estimate  {format_usd(cost.estimate.total)}  •  {format_time(timing.estimate.total)}",
            xalign=0,
        )
        estimate.add_css_class("caption")
        actual = Gtk.Label(
            label=f"Actual     {format_usd(cost.actual.total)}  •  {format_time(timing.actual.total)}",
            xalign=0,
        )
        actual.add_css_class("caption")
        actual.add_css_class("dim-label")
        metrics.append(estimate)
        metrics.append(actual)
        metrics.set_tooltip_text(
            "Estimated/actual total USD and processing time for Scribe, Translator, and Summarizer"
        )
        root.append(metrics)
        self.set_child(root)


class ZScribeWindow(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application):
        super().__init__(application=application, title="Z Scribe")
        self.set_default_size(1180, 760)
        self.paths = AppPaths()
        self.store = Store(self.paths)
        self.settings = self.store.load_settings()
        self.credentials = self.store.load_credentials()
        self.jobs = [apply_existing(job) for job in self.store.load_jobs()]
        for job in self.jobs:
            if reason := basic_validation_error(Path(job.source_path)):
                job.state = JobState.FAILED.value
                job.status_message = "Invalid source media"
                job.error = f"Cannot process {job.display_name}: {reason}. Download or export the media again."
        self.selected_job_id: str | None = None
        self.cancel_event = threading.Event()
        self.resume_event = threading.Event()
        self.resume_event.set()
        self.queue_thread: threading.Thread | None = None
        self.queue_only_job_id: str | None = None
        self.live_session: LiveSession | None = None
        self.live_final: list[str] = []
        self.live_source_final: list[str] = []
        self.live_interim = ""
        self.live_summary_running = False
        self.live_summary_cancel = threading.Event()
        self.live_event_count = 0
        self.live_last_frames = 0
        self.live_last_bytes = 0
        self.live_clip_until = 0.0
        self.floating_caption_message: str | None = None
        self.active_cue_index: int | None = None
        self.review_cues = []
        self.media_stream: Gtk.MediaFile | None = None
        self.review_playback_error: str | None = None
        self.review_duration_warning = ""
        self.review_job: QueueJob | None = None
        self.review_playback_rate = 1.0
        self.pending_review_position = 0.0
        self.resume_after_prepare = False
        self.playback_cancel = threading.Event()
        self.floating_window: Gtk.Window | None = None

        self.toast_overlay = Adw.ToastOverlay()
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.toast_overlay.set_child(root)
        self.set_content(self.toast_overlay)
        self.stack = Adw.ViewStack()
        self.stack.set_vexpand(True)
        header = Adw.HeaderBar()
        switcher = Adw.ViewSwitcher()
        switcher.set_stack(self.stack)
        switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)
        about = Gtk.Button(icon_name="help-about-symbolic", tooltip_text="About Z Scribe")
        about.connect("clicked", self._show_about)
        header.pack_end(about)
        root.append(header)
        root.append(self.stack)

        self._build_queue()
        self._build_review()
        self._build_live()
        self._build_settings()
        self._install_drop_target()
        self._refresh_queue()
        GLib.timeout_add(200, self._review_tick)

    def toast(self, message: str) -> None:
        self.toast_overlay.add_toast(Adw.Toast.new(message))

    def _build_queue(self) -> None:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        toolbar.set_margin_top(12)
        toolbar.set_margin_bottom(12)
        toolbar.set_margin_start(16)
        toolbar.set_margin_end(16)
        add_files = Gtk.Button(label="Add Files", icon_name="list-add-symbolic")
        add_files.connect("clicked", self._choose_files)
        add_folder = Gtk.Button(label="Add Folder", icon_name="folder-open-symbolic")
        add_folder.connect("clicked", self._choose_folder)
        self.start_button = Gtk.Button(label="Start Queue", icon_name="media-playback-start-symbolic")
        self.start_button.add_css_class("suggested-action")
        self.start_button.connect("clicked", self._start_or_cancel)
        start_selected = Gtk.Button(icon_name="media-playback-start-symbolic", tooltip_text="Start selected job only")
        start_selected.connect("clicked", self._start_selected)
        retry = Gtk.Button(label="Retry Failed", icon_name="view-refresh-symbolic")
        retry.connect("clicked", self._retry_failed)
        self.pause_button = Gtk.Button(label="Pause", icon_name="media-playback-pause-symbolic")
        self.pause_button.set_sensitive(False)
        self.pause_button.set_tooltip_text("Pause before the next queued item")
        self.pause_button.connect("clicked", self._pause_or_resume)
        retry_selected = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Retry selected job")
        retry_selected.connect("clicked", self._retry_selected)
        configure = Gtk.Button(icon_name="document-edit-symbolic", tooltip_text="Configure selected job")
        configure.connect("clicked", self._configure_selected)
        activity = Gtk.Button(icon_name="dialog-information-symbolic", tooltip_text="Selected job activity and usage")
        activity.connect("clicked", self._show_job_activity)
        duplicate = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="Duplicate selected job")
        duplicate.connect("clicked", self._duplicate_selected)
        remove = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Remove selected job")
        remove.connect("clicked", self._remove_selected)
        toolbar.append(add_files)
        toolbar.append(add_folder)
        toolbar.append(self.start_button)
        toolbar.append(start_selected)
        toolbar.append(self.pause_button)
        toolbar.append(retry)
        toolbar.append(retry_selected)
        toolbar.append(configure)
        toolbar.append(activity)
        toolbar.append(duplicate)
        toolbar.append(remove)

        options_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        options_bar.set_margin_start(16)
        options_bar.set_margin_end(16)
        options_bar.set_margin_bottom(12)
        options_bar.append(Gtk.Label(label="New jobs", xalign=0))
        self.source_dropdown = Gtk.DropDown.new_from_strings(list(LANGUAGES.values()))
        self.source_dropdown.set_tooltip_text("Transcription language for newly added media")
        options_bar.append(self.source_dropdown)
        translation_names = ["No translation"] + list(LANGUAGES.values())
        self.translation_dropdown = Gtk.DropDown.new_from_strings(translation_names)
        self.translation_dropdown.set_tooltip_text("Translation language for newly added media")
        options_bar.append(self.translation_dropdown)
        self.summary_check = Gtk.CheckButton(label="Summarize")
        self.summary_check.set_active(True)
        options_bar.append(self.summary_check)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        options_bar.append(spacer)
        self.search = Gtk.SearchEntry(placeholder_text="Search queue")
        self.search.set_size_request(220, -1)
        self.search.connect("search-changed", lambda *_: self._refresh_queue())
        options_bar.append(self.search)
        self.state_filter = Gtk.DropDown.new_from_strings(
            ["All jobs", "Queued", "Processing", "Ready", "Failed", "Canceled"]
        )
        self.state_filter.connect("notify::selected", lambda *_: self._refresh_queue())
        options_bar.append(self.state_filter)
        page.append(toolbar)
        page.append(options_bar)
        page.append(Gtk.Separator())

        self.queue_list = Gtk.ListBox()
        self.queue_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.queue_list.add_css_class("boxed-list")
        self.queue_list.connect("row-selected", self._queue_selected)
        self.queue_list.connect("row-activated", self._queue_activated)
        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self.queue_list)
        scroller.set_margin_start(16)
        scroller.set_margin_end(16)
        scroller.set_margin_bottom(16)
        page.append(scroller)
        self.queue_totals = Gtk.Label(xalign=1)
        self.queue_totals.set_margin_start(16)
        self.queue_totals.set_margin_end(16)
        self.queue_totals.set_margin_bottom(12)
        self.queue_totals.add_css_class("caption")
        page.append(self.queue_totals)
        self.stack.add_titled_with_icon(page, "queue", "Queue", "view-list-symbolic")

    def _build_review(self) -> None:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        top.set_margin_top(10)
        top.set_margin_bottom(10)
        top.set_margin_start(14)
        top.set_margin_end(14)
        self.review_title = Gtk.Label(label="Select a completed queue item", xalign=0)
        self.review_title.add_css_class("title-3")
        self.review_title.set_hexpand(True)
        top.append(self.review_title)
        reveal = Gtk.Button(label="Show in Files", icon_name="folder-open-symbolic")
        reveal.connect("clicked", self._reveal_selected)
        top.append(reveal)
        original_track = Gtk.Button(label="Original captions")
        original_track.connect("clicked", lambda *_: self._select_caption_track(False))
        top.append(original_track)
        self.translated_track_button = Gtk.Button(label="Translated captions")
        self.translated_track_button.connect("clicked", lambda *_: self._select_caption_track(True))
        top.append(self.translated_track_button)
        page.append(top)
        page.append(Gtk.Separator())
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_vexpand(True)
        paned.set_position(720)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.video = Gtk.Video()
        self.video.set_hexpand(True)
        self.video.set_vexpand(True)
        self.video.set_autoplay(False)
        left.append(self.video)
        self.caption_label = Gtk.Label(label="", wrap=True, justify=Gtk.Justification.CENTER)
        self.caption_label.set_size_request(-1, 64)
        self.caption_label.add_css_class("caption-overlay")
        left.append(self.caption_label)
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        controls.set_margin_top(8)
        controls.set_margin_bottom(8)
        controls.set_margin_start(12)
        controls.set_margin_end(12)
        self.play_button = Gtk.Button(icon_name="media-playback-start-symbolic")
        self.play_button.connect("clicked", self._toggle_playback)
        controls.append(self.play_button)
        self.position_label = Gtk.Label(label="00:00")
        controls.append(self.position_label)
        self.timeline = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 1)
        self.timeline.set_draw_value(False)
        self.timeline.set_hexpand(True)
        self.timeline.connect("change-value", self._seek)
        controls.append(self.timeline)
        self.duration_label = Gtk.Label(label="00:00")
        controls.append(self.duration_label)
        self.playback_rate = Gtk.DropDown.new_from_strings(["1×", "1.5×", "2×", "4×"])
        self.playback_rate.set_tooltip_text("Playback speed (prepared and cached on first use)")
        self.playback_rate.connect("notify::selected", self._set_playback_rate)
        controls.append(self.playback_rate)
        left.append(controls)
        paned.set_start_child(left)

        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        details.set_size_request(340, -1)
        detail_stack = Adw.ViewStack()
        detail_stack.set_vexpand(True)
        switcher = Adw.ViewSwitcher()
        switcher.set_stack(detail_stack)
        switcher.set_margin_top(8)
        switcher.set_margin_bottom(8)
        switcher.set_margin_start(10)
        switcher.set_margin_end(10)
        details.append(switcher)
        self.caption_list = Gtk.ListBox()
        self.caption_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.caption_list.connect("row-activated", self._caption_activated)
        caption_scroll = Gtk.ScrolledWindow()
        caption_scroll.set_child(self.caption_list)
        detail_stack.add_titled(caption_scroll, "captions", "Captions")
        self.summary_view = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.summary_view.set_left_margin(16)
        self.summary_view.set_right_margin(16)
        self.summary_view.set_top_margin(16)
        self.summary_view.set_bottom_margin(16)
        summary_scroll = Gtk.ScrolledWindow()
        summary_scroll.set_child(self.summary_view)
        detail_stack.add_titled(summary_scroll, "summary", "Summary")
        details.append(detail_stack)
        paned.set_end_child(details)
        page.append(paned)
        self.stack.add_titled_with_icon(page, "review", "Review", "media-playback-start-symbolic")

    def _build_live(self) -> None:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_margin_top(16)
        page.set_margin_bottom(16)
        page.set_margin_start(18)
        page.set_margin_end(18)
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.live_input = Gtk.DropDown.new_from_strings(
            ["Microphone", "System audio", "Microphone + system audio"]
        )
        self.live_input.set_selected(
            {"microphone": 0, "system": 1, "both": 2}.get(self.settings.live_source, 0)
        )
        self.live_input.connect("notify::selected", self._live_source_changed)
        toolbar.append(Gtk.Label(label="Input"))
        toolbar.append(self.live_input)
        self.live_microphone_label = Gtk.Label(label="Microphone")
        toolbar.append(self.live_microphone_label)
        self.live_microphone_device = Gtk.DropDown.new_from_strings(["Default microphone"])
        self.live_microphone_device.set_size_request(190, -1)
        toolbar.append(self.live_microphone_device)
        self.live_system_label = Gtk.Label(label="Loopback")
        toolbar.append(self.live_system_label)
        self.live_system_device = Gtk.DropDown.new_from_strings(["Default system monitor"])
        self.live_system_device.set_size_request(210, -1)
        toolbar.append(self.live_system_device)
        refresh_devices = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh audio devices")
        refresh_devices.connect("clicked", lambda *_: self._refresh_live_devices())
        toolbar.append(refresh_devices)
        page.append(toolbar)
        options = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.live_language = Gtk.DropDown.new_from_strings(list(LANGUAGES.values()))
        if self.settings.live_language in LANGUAGES:
            self.live_language.set_selected(list(LANGUAGES).index(self.settings.live_language))
        options.append(Gtk.Label(label="Language"))
        options.append(self.live_language)
        self.live_translation = Gtk.DropDown.new_from_strings(["No translation"] + list(LANGUAGES.values()))
        if self.settings.live_translation_language in LANGUAGES:
            self.live_translation.set_selected(list(LANGUAGES).index(self.settings.live_translation_language) + 1)
        options.append(Gtk.Label(label="Translate"))
        options.append(self.live_translation)
        self.live_auto_gain = Gtk.CheckButton(label="Auto gain")
        self.live_auto_gain.set_active(self.settings.live_auto_gain)
        options.append(self.live_auto_gain)
        page.append(options)
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.live_button = Gtk.Button(label="Start Listening", icon_name="media-record-symbolic")
        self.live_button.add_css_class("suggested-action")
        self.live_button.connect("clicked", self._toggle_live)
        actions.append(self.live_button)
        float_button = Gtk.Button(label="Caption Window", icon_name="view-restore-symbolic")
        float_button.connect("clicked", self._show_caption_window)
        actions.append(float_button)
        self.live_summarize_button = Gtk.Button(label="Summarize", icon_name="document-edit-symbolic")
        self.live_summarize_button.set_sensitive(False)
        self.live_summarize_button.set_tooltip_text("Summarize all finalized source-language speech with Zoom Summarizer")
        self.live_summarize_button.connect("clicked", self._summarize_live_transcript)
        actions.append(self.live_summarize_button)
        copy_transcript = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="Copy live transcript")
        copy_transcript.connect("clicked", self._copy_live_transcript)
        actions.append(copy_transcript)
        clear_transcript = Gtk.Button(icon_name="edit-clear-symbolic", tooltip_text="Clear live transcript")
        clear_transcript.connect("clicked", self._clear_live_transcript)
        actions.append(clear_transcript)
        page.append(actions)
        status = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.live_status = Gtk.Label(label="Stopped", xalign=0)
        self.live_status.set_hexpand(True)
        self.live_level = Gtk.LevelBar()
        self.live_level.set_min_value(0)
        self.live_level.set_max_value(1)
        self.live_level.set_size_request(180, -1)
        status.append(self.live_status)
        status.append(Gtk.Label(label="Input"))
        status.append(self.live_level)
        self.live_db_label = Gtk.Label(label="-- dBFS")
        self.live_db_label.set_width_chars(10)
        status.append(self.live_db_label)
        page.append(status)
        self.live_stats = Gtk.Label(
            label="Audio sent: 0 frames (0 bytes)  •  Zoom events: 0", xalign=0
        )
        self.live_stats.add_css_class("caption")
        self.live_stats.add_css_class("dim-label")
        page.append(self.live_stats)
        page.append(Gtk.Separator())
        page.append(Gtk.Label(label="Detected words / not yet final", xalign=0, css_classes=["caption-heading"]))
        self.live_interim_view = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.live_interim_view.set_size_request(-1, 76)
        self.live_interim_view.add_css_class("live-transcript")
        interim_scroll = Gtk.ScrolledWindow()
        interim_scroll.set_min_content_height(76)
        interim_scroll.set_child(self.live_interim_view)
        page.append(interim_scroll)
        page.append(Gtk.Label(label="Completed speech turns", xalign=0, css_classes=["caption-heading"]))
        self.live_view = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.live_view.add_css_class("live-transcript")
        live_scroll = Gtk.ScrolledWindow()
        live_scroll.set_vexpand(True)
        live_scroll.set_child(self.live_view)
        page.append(live_scroll)
        self.live_summary_expander = Adw.ExpanderRow(
            title="Live summary",
            subtitle="Summarize finalized speech captured in this Live session",
        )
        summary_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        summary_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        copy_summary = Gtk.Button(label="Copy Summary", icon_name="edit-copy-symbolic")
        copy_summary.connect("clicked", self._copy_live_summary)
        summary_actions.append(copy_summary)
        summary_box.append(summary_actions)
        self.live_summary_view = Gtk.TextView(
            editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR,
        )
        summary_scroll = Gtk.ScrolledWindow()
        summary_scroll.set_min_content_height(180)
        summary_scroll.set_child(self.live_summary_view)
        summary_box.append(summary_scroll)
        self.live_summary_expander.add_row(summary_box)
        page.append(self.live_summary_expander)
        diagnostics_expander = Adw.ExpanderRow(
            title="Zoom API diagnostics",
            subtitle="Sanitized WebSocket responses and audio-send counters",
        )
        diagnostics_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        diagnostic_actions = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8
        )
        clear_diagnostics = Gtk.Button(label="Clear")
        clear_diagnostics.connect("clicked", self._clear_live_diagnostics)
        copy_diagnostics = Gtk.Button(
            label="Copy Diagnostics", icon_name="edit-copy-symbolic"
        )
        copy_diagnostics.connect("clicked", self._copy_live_diagnostics)
        diagnostic_actions.append(clear_diagnostics)
        diagnostic_actions.append(copy_diagnostics)
        diagnostics_box.append(diagnostic_actions)
        self.live_diagnostics_view = Gtk.TextView(
            editable=False,
            cursor_visible=False,
            monospace=True,
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
        )
        try:
            previous = self.paths.live_diagnostics.read_text(encoding="utf-8")
            self.live_diagnostics_view.get_buffer().set_text(previous[-64_000:])
        except OSError:
            pass
        diagnostics_scroll = Gtk.ScrolledWindow()
        diagnostics_scroll.set_min_content_height(190)
        diagnostics_scroll.set_child(self.live_diagnostics_view)
        diagnostics_box.append(diagnostics_scroll)
        diagnostics_expander.add_row(diagnostics_box)
        page.append(diagnostics_expander)
        vocabulary_expander = Adw.ExpanderRow(title="Session vocabulary JSON", subtitle="Optional phrases, pronunciations, and aliases")
        self.vocabulary_view = Gtk.TextView(monospace=True, wrap_mode=Gtk.WrapMode.NONE)
        self.vocabulary_view.get_buffer().set_text(
            self.settings.live_vocabulary_json or DEFAULT_VOCABULARY
        )
        vocabulary_scroll = Gtk.ScrolledWindow()
        vocabulary_scroll.set_min_content_height(150)
        vocabulary_scroll.set_child(self.vocabulary_view)
        vocabulary_expander.add_row(vocabulary_scroll)
        page.append(vocabulary_expander)
        self.stack.add_titled_with_icon(page, "live", "Live", "audio-input-microphone-symbolic")
        GLib.timeout_add_seconds(1, self._live_stats_tick)
        self._refresh_live_devices()

    def _build_settings(self) -> None:
        page = Gtk.ScrolledWindow()
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        content.set_margin_top(24)
        content.set_margin_bottom(24)
        content.set_margin_start(24)
        content.set_margin_end(24)
        content.set_size_request(620, -1)
        page.set_child(content)
        credentials_group = Adw.PreferencesGroup(title="Zoom Build credentials", description="Stored locally with permissions 0600. The first service request validates access.")
        self.api_key_entry = Adw.EntryRow(title="API key")
        self.api_key_entry.set_text(self.credentials.api_key)
        self.api_secret_entry = Adw.PasswordEntryRow(title="API secret")
        self.api_secret_entry.set_text(self.credentials.api_secret)
        credentials_group.add(self.api_key_entry)
        credentials_group.add(self.api_secret_entry)
        content.append(credentials_group)
        media_group = Adw.PreferencesGroup(title="Media processing")
        self.ffmpeg_entry = Adw.EntryRow(title="FFmpeg executable")
        self.ffmpeg_entry.set_text(self.settings.ffmpeg_path)
        self.ffprobe_entry = Adw.EntryRow(title="FFprobe executable")
        self.ffprobe_entry.set_text(self.settings.ffprobe_path)
        self.concurrency_spin = Adw.SpinRow.new_with_range(1, 4, 1)
        self.concurrency_spin.set_title("Concurrent Scribe calls")
        self.concurrency_spin.set_value(self.settings.scribe_concurrency)
        self.segment_spin = Adw.SpinRow.new_with_range(1, 30, 1)
        self.segment_spin.set_title("Segment duration (minutes)")
        self.segment_spin.set_value(self.settings.segment_minutes)
        for row in (self.ffmpeg_entry, self.ffprobe_entry, self.concurrency_spin, self.segment_spin):
            media_group.add(row)
        content.append(media_group)
        rates_group = Adw.PreferencesGroup(
            title="Cost estimate rates (USD)",
            description="Override these rates when your Zoom Build account pricing differs.",
        )
        self.scribe_rate_spin = Adw.SpinRow.new_with_range(0, 100, 0.0001)
        self.scribe_rate_spin.set_title("Scribe Fast USD per audio minute")
        self.scribe_rate_spin.set_digits(4)
        self.scribe_rate_spin.set_value(self.settings.scribe_usd_per_minute)
        self.translator_rate_spin = Adw.SpinRow.new_with_range(0, 10_000, 0.01)
        self.translator_rate_spin.set_title("Translator USD per 1M characters")
        self.translator_rate_spin.set_digits(2)
        self.translator_rate_spin.set_value(self.settings.translator_usd_per_million_characters)
        self.summarizer_rate_spin = Adw.SpinRow.new_with_range(0, 10_000, 0.01)
        self.summarizer_rate_spin.set_title("Summarizer USD per 1M characters")
        self.summarizer_rate_spin.set_digits(2)
        self.summarizer_rate_spin.set_value(self.settings.summarizer_usd_per_million_characters)
        self.character_density_spin = Adw.SpinRow.new_with_range(0, 10_000, 50)
        self.character_density_spin.set_title("Characters per minute override (0 = language default)")
        self.character_density_spin.set_value(self.settings.estimated_characters_per_minute)
        for row in (
            self.scribe_rate_spin, self.translator_rate_spin,
            self.summarizer_rate_spin, self.character_density_spin,
        ):
            rates_group.add(row)
        content.append(rates_group)
        save = Gtk.Button(label="Save Settings", icon_name="document-save-symbolic")
        save.add_css_class("suggested-action")
        save.set_halign(Gtk.Align.END)
        save.connect("clicked", self._save_settings)
        content.append(save)
        self.stack.add_titled_with_icon(page, "settings", "Settings", "emblem-system-symbolic")

    def _install_drop_target(self) -> None:
        target = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.COPY)
        target.connect("drop", self._drop)
        self.add_controller(target)

    def _drop(self, _target: Gtk.DropTarget, value: str, _x: float, _y: float) -> bool:
        paths = []
        for line in value.splitlines():
            if line.startswith("file://"):
                paths.append(Path(unquote(urlparse(line).path)))
        if paths:
            self._add_paths(paths)
            return True
        return False

    def _selected_languages(self) -> tuple[str, str]:
        locales = list(LANGUAGES)
        source = locales[self.source_dropdown.get_selected()]
        translated_index = self.translation_dropdown.get_selected()
        target = "" if translated_index == 0 else locales[translated_index - 1]
        return source, target

    def _choose_files(self, _button: Gtk.Button) -> None:
        dialog = Gtk.FileDialog(title="Add media")
        media_filter = Gtk.FileFilter()
        media_filter.set_name("Audio and video")
        for extension in sorted(MEDIA_EXTENSIONS):
            media_filter.add_suffix(extension.removeprefix("."))
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(media_filter)
        dialog.set_filters(filters)
        dialog.set_default_filter(media_filter)
        self._file_dialog = dialog
        dialog.open_multiple(self, None, self._files_opened)

    def _files_opened(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            files = dialog.open_multiple_finish(result)
        except GLib.Error as error:
            if not error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                self.toast(f"Could not open the file picker: {error.message}")
            return
        finally:
            self._file_dialog = None
        paths = self._local_file_paths(files)
        if paths:
            self._add_paths(paths)

    def _choose_folder(self, _button: Gtk.Button) -> None:
        dialog = Gtk.FileDialog(title="Add media folder")
        self._folder_dialog = dialog
        dialog.select_folder(self, None, self._folder_opened)

    def _folder_opened(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            selected = dialog.select_folder_finish(result)
        except GLib.Error as error:
            if not error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                self.toast(f"Could not open the folder picker: {error.message}")
            return
        finally:
            self._folder_dialog = None
        paths = self._local_file_paths([selected])
        if paths:
            self._add_paths(paths)

    def _local_file_paths(self, files) -> list[Path]:
        count = files.get_n_items() if hasattr(files, "get_n_items") else len(files)
        values = [files.get_item(index) for index in range(count)] if hasattr(files, "get_item") else list(files)
        paths = [Path(value.get_path()) for value in values if value and value.get_path()]
        if len(paths) != len(values):
            self.toast("Only local files and folders can be added")
        return paths

    def _add_paths(self, paths: list[Path]) -> None:
        source, target = self._selected_languages()
        existing = {Path(job.source_path).resolve() for job in self.jobs}
        added = 0
        rejected: list[tuple[Path, str]] = []
        for path in discover(paths):
            if path in existing:
                continue
            if reason := basic_validation_error(path):
                rejected.append((path, reason))
                continue
            job = apply_existing(QueueJob(str(path), source, target, self.summary_check.get_active()))
            self.jobs.append(job)
            existing.add(path)
            added += 1
            threading.Thread(
                target=self._probe_added_job, args=(job.id,), daemon=True
            ).start()
        self.store.save_jobs(self.jobs)
        self._refresh_queue()
        message = f"Added {added} media file{'s' if added != 1 else ''}"
        if rejected:
            first_path, first_reason = rejected[0]
            message += f"; rejected {len(rejected)} invalid file{'s' if len(rejected) != 1 else ''} ({first_path.name}: {first_reason})"
        self.toast(message)

    def _refresh_queue(self) -> None:
        selected = self.selected_job_id
        calibration = learn_time_calibration(self.jobs)
        while child := self.queue_list.get_first_child():
            self.queue_list.remove(child)
        query = self.search.get_text().strip().lower() if hasattr(self, "search") else ""
        selected_filter = self.state_filter.get_selected() if hasattr(self, "state_filter") else 0
        for job in self.jobs:
            haystack = f"{job.display_name} {job.status_message} {job.state}".lower()
            if query:
                for path in (job.original_vtt_path, job.translated_vtt_path, job.transcript_json_path, job.summary_path):
                    if path and Path(path).is_file():
                        try:
                            haystack += " " + Path(path).read_text(encoding="utf-8").lower()
                        except OSError:
                            pass
            filter_matches = (
                selected_filter == 0
                or selected_filter == 1 and job.state == JobState.QUEUED.value
                or selected_filter == 2 and JobState(job.state).processing
                or selected_filter == 3 and job.state == JobState.READY.value
                or selected_filter == 4 and job.state == JobState.FAILED.value
                or selected_filter == 5 and job.state == JobState.CANCELED.value
            )
            if not filter_matches or (query and query not in haystack):
                continue
            row = JobRow(job, self.settings, calibration)
            self.queue_list.append(row)
            if row.job_id == selected:
                self.queue_list.select_row(row)
        costs = [cost_comparison(job, self.settings) for job in self.jobs]
        times = [time_comparison(job, self.settings, calibration) for job in self.jobs]
        estimated_cost = sum_breakdowns([item.estimate for item in costs])
        actual_cost = sum_breakdowns([item.actual for item in costs])
        estimated_time = sum_breakdowns([item.estimate for item in times])
        actual_time = sum_breakdowns([item.actual for item in times])
        self.queue_totals.set_label(
            f"Queue estimate: {format_usd(estimated_cost.total)} • {format_time(estimated_time.total)}    "
            f"Actual: {format_usd(actual_cost.total)} • {format_time(actual_time.total)}"
        )
        self.start_button.set_sensitive(any(job.state == JobState.QUEUED.value for job in self.jobs) or bool(self.queue_thread and self.queue_thread.is_alive()))
        if self.queue_thread and self.queue_thread.is_alive():
            self.start_button.set_label("Cancel Current")
            self.start_button.set_icon_name("media-playback-stop-symbolic")
            self.pause_button.set_sensitive(True)
            if self.resume_event.is_set():
                self.pause_button.set_label("Pause")
                self.pause_button.set_icon_name("media-playback-pause-symbolic")
            else:
                self.pause_button.set_label("Resume")
                self.pause_button.set_icon_name("media-playback-start-symbolic")
        else:
            self.start_button.set_label("Start Queue")
            self.start_button.set_icon_name("media-playback-start-symbolic")
            self.pause_button.set_sensitive(False)
            self.pause_button.set_label("Pause")
            self.pause_button.set_icon_name("media-playback-pause-symbolic")

    def _queue_selected(self, _list: Gtk.ListBox, row: JobRow | None) -> None:
        self.selected_job_id = row.job_id if row else None

    def _queue_activated(self, _list: Gtk.ListBox, row: JobRow) -> None:
        job = self._job(row.job_id)
        if job and job.can_review:
            self._load_review(job)
            self.stack.set_visible_child_name("review")
        elif job:
            self._load_review(job)
            self.stack.set_visible_child_name("review")
            if job.error:
                self.toast(f"Previewing source media; processing error: {job.error}")

    def _start_or_cancel(self, _button: Gtk.Button) -> None:
        if self.queue_thread and self.queue_thread.is_alive():
            self.cancel_event.set()
            self.toast("Canceling the current job…")
            return
        self._begin_queue()

    def _start_selected(self, _button: Gtk.Button) -> None:
        if self.queue_thread and self.queue_thread.is_alive():
            self.toast("A queue operation is already running")
            return
        job = self._job(self.selected_job_id)
        if not job:
            self.toast("Select a queued item to start")
            return
        if job.state != JobState.QUEUED.value:
            self.toast("The selected item is not queued")
            return
        self._begin_queue(job.id)

    def _begin_queue(self, only_job_id: str | None = None) -> None:
        if not self.credentials.complete:
            self.stack.set_visible_child_name("settings")
            self.toast("Save Zoom Build credentials first")
            return
        if not tool_available(self.settings.ffmpeg_path) or not tool_available(self.settings.ffprobe_path):
            self.stack.set_visible_child_name("settings")
            self.toast("Install FFmpeg and FFprobe, or set their paths")
            return
        self.cancel_event = threading.Event()
        self.resume_event.set()
        self.queue_only_job_id = only_job_id
        self.queue_thread = threading.Thread(target=self._run_queue, daemon=True, name="zscribe-queue")
        self.queue_thread.start()
        self._refresh_queue()

    def _run_queue(self) -> None:
        pipeline = MediaPipeline(self.paths)
        for job in self.jobs:
            if self.queue_only_job_id and job.id != self.queue_only_job_id:
                continue
            if job.state != JobState.QUEUED.value:
                continue
            while not self.resume_event.wait(0.2):
                if self.cancel_event.is_set():
                    break
            if self.cancel_event.is_set():
                break
            try:
                pipeline.process(job, copy.copy(self.settings), self.credentials, self._thread_update, self.cancel_event)
            except CancelledError:
                job.report(JobState.CANCELED, job.progress, "Canceled by user")
                self._thread_update(job)
                break
            except Exception as error:
                job.error = str(error)
                job.report(JobState.FAILED, job.progress, "Processing failed")
                self._thread_update(job)
        GLib.idle_add(self._queue_finished)

    def _thread_update(self, updated: QueueJob) -> None:
        snapshot = QueueJob.from_dict(updated.as_dict())
        GLib.idle_add(self._apply_update, snapshot)

    def _apply_update(self, updated: QueueJob) -> bool:
        for index, job in enumerate(self.jobs):
            if job.id == updated.id:
                self.jobs[index] = updated
                break
        self.store.save_jobs(self.jobs)
        self._refresh_queue()
        return False

    def _queue_finished(self) -> bool:
        self.queue_thread = None
        self.queue_only_job_id = None
        self.resume_event.set()
        self._refresh_queue()
        return False

    def _pause_or_resume(self, _button: Gtk.Button) -> None:
        if not self.queue_thread or not self.queue_thread.is_alive():
            return
        if self.resume_event.is_set():
            self.resume_event.clear()
            self.toast("Queue paused after the current item")
        else:
            self.resume_event.set()
            self.toast("Queue resumed")
        self._refresh_queue()

    def _retry_failed(self, _button: Gtk.Button) -> None:
        count = 0
        for job in self.jobs:
            if job.state in {JobState.FAILED.value, JobState.CANCELED.value}:
                job.state = JobState.QUEUED.value
                job.error = None
                job.progress = 0
                job.status_message = "Waiting to retry"
                count += 1
        self.store.save_jobs(self.jobs)
        self._refresh_queue()
        self.toast(f"Queued {count} job{'s' if count != 1 else ''} for retry")

    def _retry_selected(self, _button: Gtk.Button) -> None:
        job = self._job(self.selected_job_id)
        if not job:
            self.toast("Select a queue item to retry")
            return
        if job.state not in {JobState.FAILED.value, JobState.CANCELED.value}:
            self.toast("Only failed or canceled items can be retried")
            return
        job.state = JobState.QUEUED.value
        job.error = None
        job.progress = 0
        job.status_message = "Waiting to retry"
        self.store.save_jobs(self.jobs)
        self._refresh_queue()
        self.toast(f"Queued {job.display_name} for retry")

    def _configure_selected(self, _button: Gtk.Button) -> None:
        job = self._job(self.selected_job_id)
        if not job:
            self.toast("Select a queue item to configure")
            return
        if JobState(job.state).processing:
            self.toast("This item cannot be changed while it is processing")
            return
        locales = list(LANGUAGES)
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading=f"Configure {job.display_name}",
            body="Changes reuse completed caption files where possible.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("apply", "Apply")
        dialog.set_response_appearance("apply", Adw.ResponseAppearance.SUGGESTED)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(8)
        source = Gtk.DropDown.new_from_strings(list(LANGUAGES.values()))
        source.set_selected(locales.index(job.source_language) if job.source_language in LANGUAGES else 0)
        source.set_sensitive(job.state in {JobState.QUEUED.value, JobState.FAILED.value, JobState.CANCELED.value})
        target = Gtk.DropDown.new_from_strings(["No translation"] + list(LANGUAGES.values()))
        target.set_selected(locales.index(job.translation_language) + 1 if job.translation_language in LANGUAGES else 0)
        summarize = Gtk.CheckButton(label="Generate summary")
        summarize.set_active(job.summarize)
        for title, control in (("Spoken language", source), ("Translation", target)):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            label = Gtk.Label(label=title, xalign=0)
            label.set_hexpand(True)
            row.append(label)
            row.append(control)
            box.append(row)
        box.append(summarize)
        dialog.set_extra_child(box)
        dialog.connect("response", self._configured_job, job.id, source, target, summarize)
        self._configure_dialog = dialog
        dialog.present()

    def _configured_job(
        self, dialog: Adw.MessageDialog, response: str, identifier: str,
        source: Gtk.DropDown, target: Gtk.DropDown, summarize: Gtk.CheckButton,
    ) -> None:
        if response != "apply":
            return
        job = self._job(identifier)
        if not job or JobState(job.state).processing:
            return
        locales = list(LANGUAGES)
        new_source = locales[source.get_selected()]
        target_index = target.get_selected()
        new_target = "" if target_index == 0 else locales[target_index - 1]
        if new_target == new_source:
            self.toast("Translation language must differ from the spoken language")
            return
        source_changed = new_source != job.source_language
        translation_changed = new_target != job.translation_language
        summary_changed = summarize.get_active() != job.summarize
        if source_changed and job.state not in {JobState.QUEUED.value, JobState.FAILED.value, JobState.CANCELED.value}:
            self.toast("Spoken language can only change before transcription starts")
            return
        if source_changed:
            job.source_language = new_source
            job.original_vtt_path = None
            job.transcript_json_path = None
            job.transcript_characters = 0
            job.reuse_existing_transcript = False
            job.existing_transcript_is_stale = True
            job.existing_translation_is_stale = True
            job.existing_summary_is_stale = True
        if translation_changed or source_changed:
            job.translation_language = new_target
            job.translated_vtt_path = None
            job.translation_input_characters = 0
            job.translation_output_characters = 0
            job.reuse_existing_translation = False
            job.existing_summary_is_stale = True
        if summary_changed or translation_changed or source_changed:
            job.summarize = summarize.get_active()
            job.summary_path = None
            job.summary_input_characters = 0
            job.summary_output_characters = 0
            job.reuse_existing_summary = False
        if source_changed or translation_changed or (summary_changed and job.summarize):
            if not source_changed and job.original_vtt_path and Path(job.original_vtt_path).is_file():
                job.reuse_existing_transcript = True
            job.state = JobState.QUEUED.value
            job.progress = 0
            job.error = None
            job.status_message = "Waiting with updated options"
        self.store.save_jobs(self.jobs)
        self._refresh_queue()
        self.toast("Queue item updated")

    def _show_job_activity(self, _button: Gtk.Button) -> None:
        job = self._job(self.selected_job_id)
        if not job:
            self.toast("Select a queue item to view activity")
            return
        costs = cost_comparison(job, self.settings)
        timing = time_comparison(job, self.settings, learn_time_calibration(self.jobs))
        def breakdown(label: str, item) -> str:
            formatter = format_usd if label == "Cost" else format_time
            return (
                f"{label}: Scribe {formatter(item.scribe)}, "
                f"Translate {formatter(item.translate)}, Summary {formatter(item.summarize)}, "
                f"Total {formatter(item.total)}"
            )
        lines = [
            str(Path(job.source_path)), "",
            breakdown("Cost", costs.estimate).replace("Cost:", "Estimated cost:"),
            breakdown("Cost", costs.actual).replace("Cost:", "Actual cost:"),
            breakdown("Time", timing.estimate).replace("Time:", "Estimated time:"),
            breakdown("Time", timing.actual).replace("Time:", "Actual time:"),
            "", "Activity:",
        ]
        lines.extend(
            f"{event.get('at', '')}  [{event.get('stage', '')}]  {event.get('message', '')}"
            for event in job.events
        )
        if not job.events:
            lines.append("No processing events yet.")
        dialog = Adw.MessageDialog(
            transient_for=self, heading=job.display_name,
            body="Processing history and Zoom usage estimates",
        )
        dialog.add_response("close", "Close")
        view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        view.get_buffer().set_text("\n".join(lines))
        scroller = Gtk.ScrolledWindow()
        scroller.set_min_content_width(760)
        scroller.set_min_content_height(360)
        scroller.set_child(view)
        dialog.set_extra_child(scroller)
        self._activity_dialog = dialog
        dialog.present()

    def _duplicate_selected(self, _button: Gtk.Button) -> None:
        source = self._job(self.selected_job_id)
        if not source:
            self.toast("Select a queue item to duplicate")
            return
        duplicate = QueueJob(
            source.source_path,
            source.source_language,
            source.translation_language,
            source.summarize,
        )
        self.jobs.append(apply_existing(duplicate))
        self.store.save_jobs(self.jobs)
        self._refresh_queue()
        self.toast("Duplicated queue item")

    def _remove_selected(self, _button: Gtk.Button) -> None:
        job = self._job(self.selected_job_id)
        if not job:
            self.toast("Select a queue item to remove")
            return
        if JobState(job.state).processing:
            self.toast("Cancel processing before removing this item")
            return
        self.jobs = [item for item in self.jobs if item.id != job.id]
        self.selected_job_id = None
        self.store.save_jobs(self.jobs)
        self._refresh_queue()
        self.toast("Removed queue item; media and sidecars were kept")

    def _job(self, identifier: str | None) -> QueueJob | None:
        return next((job for job in self.jobs if job.id == identifier), None)

    def _probe_added_job(self, identifier: str) -> None:
        job = self._job(identifier)
        if not job or job.duration_seconds is not None:
            return
        try:
            result = media_probe(Path(job.source_path), copy.copy(self.settings))
            updated = QueueJob.from_dict(job.as_dict())
            updated.duration_seconds = result.duration
            updated.has_audio = bool(result.audio_codec)
            GLib.idle_add(self._apply_update, updated)
        except Exception:
            # The processing pipeline preserves the actionable probe failure.
            pass

    def _load_review(self, job: QueueJob) -> None:
        self.selected_job_id = job.id
        self.review_job = job
        self.review_title.set_label(job.display_name)
        self.translated_track_button.set_sensitive(bool(job.translated_vtt_path))
        self._select_caption_track(False)
        summary = "No summary was generated for this job."
        if job.summary_path and Path(job.summary_path).is_file():
            summary = Path(job.summary_path).read_text(encoding="utf-8")
        self.summary_view.get_buffer().set_text(summary)
        self.playback_cancel.set()
        self.playback_cancel = threading.Event()
        self.pending_review_position = 0.0
        self.resume_after_prepare = False
        self.media_stream = None
        self.video.set_media_stream(None)
        self.caption_label.set_label("Preparing compatible playback media…")
        threading.Thread(
            target=self._prepare_playback,
            args=(job.id, Path(job.source_path), self.playback_cancel, self.review_playback_rate),
            daemon=True,
        ).start()

    def _select_caption_track(self, translated: bool) -> None:
        job = self.review_job
        if not job:
            return
        caption_path = job.translated_vtt_path if translated else job.original_vtt_path
        self.review_cues = vtt.parse(Path(caption_path).read_text(encoding="utf-8")) if caption_path else []
        while child := self.caption_list.get_first_child():
            self.caption_list.remove(child)
        for cue in self.review_cues:
            row = Gtk.ListBoxRow(activatable=True)
            row.cue_index = cue.index
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            box.set_margin_top(8); box.set_margin_bottom(8); box.set_margin_start(10); box.set_margin_end(10)
            time_label = Gtk.Label(label=clock(cue.start), xalign=0)
            time_label.add_css_class("accent")
            body = Gtk.Label(label=cue.text, xalign=0, wrap=True)
            box.append(time_label); box.append(body); row.set_child(box)
            self.caption_list.append(row)

    def _prepare_playback(
        self, identifier: str, source: Path, cancel: threading.Event, rate: float,
    ) -> None:
        try:
            resolved = PlaybackResolver(self.paths).resolve(
                source, copy.copy(self.settings), cancel, rate,
            )
            GLib.idle_add(self._playback_prepared, identifier, rate, str(resolved), None)
        except CancelledError:
            return
        except Exception as error:
            GLib.idle_add(self._playback_prepared, identifier, rate, "", str(error))

    def _playback_prepared(
        self, identifier: str, rate: float, path: str, error: str | None,
    ) -> bool:
        if not self.review_job or self.review_job.id != identifier or rate != self.review_playback_rate:
            return False
        if error:
            self.review_playback_error = error
            self.caption_label.set_label(f"Playback preparation failed: {error}")
            return False
        self.media_stream = Gtk.MediaFile.new_for_filename(path)
        self.review_playback_error = None
        self.video.set_media_stream(self.media_stream)
        if self.pending_review_position:
            self.media_stream.seek(int(self.pending_review_position / rate * 1_000_000))
        if self.resume_after_prepare:
            self.media_stream.set_playing(True)
        job = self.review_job
        if job.duration_seconds is not None and job.duration_seconds < 1:
            self.review_duration_warning = (
                f"This media contains only {job.duration_seconds:.2f} seconds and may be too short to play."
            )
            self.caption_label.set_label(self.review_duration_warning)
        else:
            self.review_duration_warning = ""
            self.caption_label.set_label("")
        return False

    def _set_playback_rate(self, *_args) -> None:
        rates = (1.0, 1.5, 2.0, 4.0)
        selected = min(self.playback_rate.get_selected(), len(rates) - 1)
        rate = rates[selected]
        if rate == self.review_playback_rate:
            return
        old_rate = self.review_playback_rate
        self.pending_review_position = (
            max(0, self.media_stream.get_timestamp() / 1_000_000) * old_rate
            if self.media_stream else 0.0
        )
        self.resume_after_prepare = bool(self.media_stream and self.media_stream.get_playing())
        self.review_playback_rate = rate
        if not self.review_job:
            return
        self.playback_cancel.set()
        self.playback_cancel = threading.Event()
        self.media_stream = None
        self.video.set_media_stream(None)
        self.caption_label.set_label(f"Preparing {rate:g}× compatible playback media…")
        threading.Thread(
            target=self._prepare_playback,
            args=(self.review_job.id, Path(self.review_job.source_path), self.playback_cancel, rate),
            daemon=True,
        ).start()

    def _toggle_playback(self, _button: Gtk.Button) -> None:
        if self.media_stream:
            self.media_stream.set_playing(not self.media_stream.get_playing())

    def _seek(self, _scale: Gtk.Scale, _scroll: Gtk.ScrollType, value: float) -> bool:
        if self.media_stream:
            self.media_stream.seek(int(value / self.review_playback_rate * 1_000_000))
        return False

    def _caption_activated(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        cue = next((item for item in self.review_cues if item.index == row.cue_index), None)
        if cue and self.media_stream:
            self.media_stream.seek(int(cue.start / self.review_playback_rate * 1_000_000))
            self.media_stream.set_playing(True)

    def _review_tick(self) -> bool:
        if self.media_stream:
            if error := self.media_stream.get_error():
                message = f"Playback failed: {error.message}. The source media may be damaged."
                if self.review_playback_error != message:
                    self.review_playback_error = message
                    self.toast(message)
                self.caption_label.set_label(message)
                self.play_button.set_icon_name("media-playback-start-symbolic")
                return True
            duration = max(0, self.media_stream.get_duration() / 1_000_000) * self.review_playback_rate
            position = max(0, self.media_stream.get_timestamp() / 1_000_000) * self.review_playback_rate
            self.timeline.set_range(0, max(1, duration))
            if not self.timeline.has_focus():
                self.timeline.set_value(position)
            self.position_label.set_label(clock(position))
            self.duration_label.set_label(clock(duration))
            self.play_button.set_icon_name("media-playback-pause-symbolic" if self.media_stream.get_playing() else "media-playback-start-symbolic")
            cue = next((item for item in self.review_cues if item.start <= position < item.end), None)
            self.caption_label.set_label(
                cue.text if cue else self.review_duration_warning
            )
        return True

    def _reveal_selected(self, _button: Gtk.Button) -> None:
        job = self._job(self.selected_job_id)
        if job:
            subprocess.Popen(["xdg-open", str(Path(job.source_path).parent)])

    def _save_settings(self, _button: Gtk.Button) -> None:
        credentials = Credentials(self.api_key_entry.get_text().strip(), self.api_secret_entry.get_text().strip())
        if bool(credentials.api_key) != bool(credentials.api_secret):
            self.toast("Both API key and API secret are required")
            return
        self.credentials = credentials
        self.settings.ffmpeg_path = self.ffmpeg_entry.get_text().strip() or "ffmpeg"
        self.settings.ffprobe_path = self.ffprobe_entry.get_text().strip() or "ffprobe"
        self.settings.scribe_concurrency = int(self.concurrency_spin.get_value())
        self.settings.segment_minutes = int(self.segment_spin.get_value())
        self.settings.scribe_usd_per_minute = self.scribe_rate_spin.get_value()
        self.settings.translator_usd_per_million_characters = self.translator_rate_spin.get_value()
        self.settings.summarizer_usd_per_million_characters = self.summarizer_rate_spin.get_value()
        self.settings.estimated_characters_per_minute = int(self.character_density_spin.get_value())
        self.store.save_credentials(self.credentials)
        self.store.save_settings(self.settings)
        self.toast("Settings saved securely")

    def _toggle_live(self, _button: Gtk.Button) -> None:
        if self.live_session:
            self.live_session.stop()
            self.live_button.set_label("Stopping…")
            self.live_button.set_sensitive(False)
            return
        locales = list(LANGUAGES)
        language = locales[self.live_language.get_selected()]
        start, end = self.vocabulary_view.get_buffer().get_bounds()
        vocabulary = self.vocabulary_view.get_buffer().get_text(start, end, True)
        callbacks = LiveCallbacks(
            event=lambda event: GLib.idle_add(self._live_event, event),
            level=lambda level: GLib.idle_add(self._set_live_level, level),
            state=lambda state: GLib.idle_add(self._live_state, state),
            error=lambda error: GLib.idle_add(self._live_error, error),
        )
        source = ("microphone", "system", "both")[self.live_input.get_selected()]
        microphone_index = self.live_microphone_device.get_selected()
        system_index = self.live_system_device.get_selected()
        microphone_id = (
            self.live_microphone_options[microphone_index].device_id
            if microphone_index < len(self.live_microphone_options) else ""
        )
        system_id = (
            self.live_system_options[system_index].device_id
            if system_index < len(self.live_system_options) else ""
        )
        device_id = system_id if source == "system" else microphone_id
        secondary_device_id = system_id if source == "both" else ""
        target_index = self.live_translation.get_selected()
        target_language = "" if target_index == 0 else locales[target_index - 1]
        self.settings.live_source = source
        self.settings.live_device_id = device_id
        self.settings.live_microphone_device_id = microphone_id
        self.settings.live_system_device_id = system_id
        self.settings.live_language = language
        self.settings.live_translation_language = target_language
        self.settings.live_vocabulary_json = vocabulary
        self.settings.live_auto_gain = self.live_auto_gain.get_active()
        self.store.save_settings(self.settings)
        self._cancel_live_summary()
        self.live_final = []
        self.live_source_final = []
        self.live_interim = ""
        self.live_summary_view.get_buffer().set_text("")
        self.live_summary_expander.set_expanded(False)
        self.live_event_count = 0
        self.live_last_frames = 0
        self.live_last_bytes = 0
        self._clear_live_diagnostics()
        self._append_live_diagnostic(
            f"Starting Zoom Live session; input={source}; language={language}"
        )
        try:
            self.live_session = LiveSession(
                self.credentials, language, vocabulary, callbacks,
                capture_source=source,
                capture_device_id=device_id,
                automatic_gain=self.live_auto_gain.get_active(),
                secondary_device_id=secondary_device_id,
            )
            self.live_session.start()
        except Exception as error:
            self.live_session = None
            self._live_error(str(error))
            return
        self.live_button.set_label("Stop Listening")
        self.live_button.set_icon_name("media-playback-stop-symbolic")

    def _live_event(self, event: dict) -> bool:
        event_type = event.get("type", "")
        self.live_event_count += 1
        raw = self._sanitized_event(event.get("raw", {}))
        serialized = json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
        if len(serialized) > 4_000:
            serialized = serialized[:4_000] + "…"
        self._append_live_diagnostic(f"← {event_type}: {serialized}")
        if event_type == "session.created":
            self.live_status.set_label("Connected — configuring Zoom Live")
        elif event_type == "session.updated":
            self.live_status.set_label("Listening")
        elif event_type.endswith("speech_started"):
            self.live_status.set_label("Speech detected")
        elif event_type.endswith("speech_stopped"):
            self.live_status.set_label("Transcribing speech turn…")
        elif event_type == "session.closed":
            self.live_status.set_label("Session closed")
        text = event.get("transcript")
        if text:
            if event_type == "transcription.completed":
                if not self.live_source_final or self.live_source_final[-1] != text:
                    self.live_final.append(text)
                    self.live_source_final.append(text)
                    target_index = self.live_translation.get_selected()
                    if target_index:
                        locales = list(LANGUAGES)
                        source = locales[self.live_language.get_selected()]
                        target = locales[target_index - 1]
                        item_index = len(self.live_final) - 1
                        threading.Thread(
                            target=self._translate_live,
                            args=(item_index, text, source, target),
                            daemon=True,
                        ).start()
                self.live_interim = ""
                self.live_status.set_label("Listening")
            else:
                self.live_interim = text
                self.live_status.set_label("Receiving captions")
            self._render_live()
        if event.get("error"):
            self.toast(event["error"])
        return False

    def _live_error(self, error: str) -> bool:
        self._append_live_diagnostic(f"ERROR: {error}")
        self.live_status.set_label(f"Live error: {error}")
        self.floating_caption_message = f"Live error\n{error[:300]}"
        self._render_floating_caption()
        self.toast(error)
        return False

    def _live_stats_tick(self) -> bool:
        session = self.live_session
        frames = session.frames_sent if session else self.live_last_frames
        byte_count = session.bytes_sent if session else self.live_last_bytes
        self.live_stats.set_label(
            f"Audio sent: {frames:,} frames ({byte_count:,} bytes)  •  "
            f"Zoom events: {self.live_event_count:,}"
        )
        return True

    def _append_live_diagnostic(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}"
        buffer = self.live_diagnostics_view.get_buffer()
        end = buffer.get_end_iter()
        prefix = "" if buffer.get_char_count() == 0 else "\n"
        buffer.insert(end, f"{prefix}{line}")
        self.paths.live_diagnostics.parent.mkdir(parents=True, exist_ok=True)
        with self.paths.live_diagnostics.open("a", encoding="utf-8") as output:
            output.write(line + "\n")
        os.chmod(self.paths.live_diagnostics, 0o600)

    def _clear_live_diagnostics(self, _button: Gtk.Button | None = None) -> None:
        self.live_diagnostics_view.get_buffer().set_text("")
        self.paths.live_diagnostics.parent.mkdir(parents=True, exist_ok=True)
        self.paths.live_diagnostics.write_text("", encoding="utf-8")
        os.chmod(self.paths.live_diagnostics, 0o600)

    def _copy_live_diagnostics(self, _button: Gtk.Button) -> None:
        buffer = self.live_diagnostics_view.get_buffer()
        start, end = buffer.get_bounds()
        text = buffer.get_text(start, end, True)
        Gdk.Display.get_default().get_clipboard().set(text)
        self.toast("Live diagnostics copied")

    @classmethod
    def _sanitized_event(cls, value):
        sensitive = {
            "authorization", "api_key", "api_secret", "access_token",
            "refresh_token", "token", "secret",
        }
        if isinstance(value, dict):
            return {
                key: "<redacted>" if key.lower() in sensitive else cls._sanitized_event(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [cls._sanitized_event(item) for item in value]
        return value

    def _translate_live(
        self, item_index: int, text: str, source: str, target: str
    ) -> None:
        try:
            translated = text
            client = ZoomClient()
            for step_source, step_target in translation_route(source, target):
                translated, _, _ = client.translate_text(
                    translated, step_source, step_target, self.credentials
                )
            GLib.idle_add(self._apply_live_translation, item_index, text, translated)
        except Exception as error:
            GLib.idle_add(self.toast, f"Live translation failed: {error}")

    def _apply_live_translation(
        self, item_index: int, original: str, translated: str
    ) -> bool:
        if item_index < len(self.live_final) and self.live_final[item_index] == original:
            self.live_final[item_index] = f"{original}\n{translated}"
            self._render_live()
        return False

    def _render_live(self) -> None:
        body = completed_transcript_display(self.live_final)
        self.live_interim_view.get_buffer().set_text(self.live_interim)
        buffer = self.live_view.get_buffer()
        buffer.set_text(body)
        self.live_view.scroll_to_iter(buffer.get_start_iter(), 0.0, False, 0, 0)
        self.live_summarize_button.set_sensitive(
            bool(self.live_source_final) and not self.live_summary_running
        )
        self.floating_caption_message = None
        self._render_floating_caption()

    def _copy_live_transcript(self, _button: Gtk.Button) -> None:
        body = "\n\n".join(self.live_final)
        if self.live_interim:
            body += ("\n\n" if body else "") + self.live_interim
        Gdk.Display.get_default().get_clipboard().set(body)
        self.toast("Live transcript copied")

    def _clear_live_transcript(self, _button: Gtk.Button) -> None:
        self._cancel_live_summary()
        self.live_final = []
        self.live_source_final = []
        self.live_interim = ""
        self.live_summary_view.get_buffer().set_text("")
        self.live_summary_expander.set_expanded(False)
        self._render_live()
        self.toast("Live transcript cleared")

    def _cancel_live_summary(self) -> None:
        self.live_summary_cancel.set()
        self.live_summary_running = False
        if hasattr(self, "live_summarize_button"):
            self.live_summarize_button.set_label("Summarize")

    def _summarize_live_transcript(self, _button: Gtk.Button) -> None:
        if self.live_summary_running:
            return
        transcript = completed_transcript_source(self.live_source_final)
        if not transcript:
            self.toast("Wait for at least one completed speech turn")
            return
        if not self.credentials.complete:
            self.stack.set_visible_child_name("settings")
            self.toast("Save Zoom Build credentials first")
            return
        locales = list(LANGUAGES)
        language = locales[self.live_language.get_selected()]
        self.live_summary_running = True
        self.live_summary_cancel.set()
        self.live_summary_cancel = threading.Event()
        self.live_summarize_button.set_label("Summarizing…")
        self.live_summarize_button.set_sensitive(False)
        self.live_summary_view.get_buffer().set_text(
            f"Sending {len(self.live_source_final)} finalized speech turn(s) to Zoom Summarizer…"
        )
        self.live_summary_expander.set_expanded(True)
        threading.Thread(
            target=self._request_live_summary,
            args=(transcript, language, self.live_summary_cancel),
            daemon=True,
            name="zscribe-live-summary",
        ).start()

    def _request_live_summary(
        self, transcript: str, language: str, cancel: threading.Event,
    ) -> None:
        try:
            summary, used_in, used_out = ZoomClient().summarize(
                transcript, language, self.credentials, cancel,
            )
            GLib.idle_add(
                self._live_summary_completed, cancel, summary, used_in, used_out,
            )
        except CancelledError:
            return
        except Exception as error:
            GLib.idle_add(self._live_summary_failed, cancel, str(error))

    def _live_summary_completed(
        self, cancel: threading.Event, summary: str, used_in: int, used_out: int,
    ) -> bool:
        if cancel.is_set() or cancel is not self.live_summary_cancel:
            return False
        self.live_summary_running = False
        self.live_summarize_button.set_label("Summarize")
        self.live_summarize_button.set_sensitive(bool(self.live_source_final))
        self.live_summary_view.get_buffer().set_text(summary)
        self.live_summary_expander.set_subtitle(
            f"Zoom Summarizer usage: {used_in:,} input and {used_out:,} output characters"
        )
        self.live_summary_expander.set_expanded(True)
        self.toast("Live transcript summary is ready")
        return False

    def _live_summary_failed(self, cancel: threading.Event, error: str) -> bool:
        if cancel.is_set() or cancel is not self.live_summary_cancel:
            return False
        self.live_summary_running = False
        self.live_summarize_button.set_label("Summarize")
        self.live_summarize_button.set_sensitive(bool(self.live_source_final))
        self.live_summary_view.get_buffer().set_text(f"Summarization failed: {error}")
        self.live_summary_expander.set_subtitle("Zoom Summarizer request failed")
        self.toast(f"Live summarization failed: {error}")
        return False

    def _copy_live_summary(self, _button: Gtk.Button) -> None:
        buffer = self.live_summary_view.get_buffer()
        start, end = buffer.get_bounds()
        Gdk.Display.get_default().get_clipboard().set(buffer.get_text(start, end, True))
        self.toast("Live summary copied")

    def _live_source_changed(self, *_args) -> None:
        self._refresh_live_devices()

    def _refresh_live_devices(self) -> None:
        source = ("microphone", "system", "both")[self.live_input.get_selected()]
        self.live_microphone_options = audio_device_options("microphone")
        self.live_system_options = audio_device_options("system")
        self.live_microphone_device.set_model(Gtk.StringList.new(
            [option.name for option in self.live_microphone_options] or ["No microphone found"]
        ))
        self.live_system_device.set_model(Gtk.StringList.new(
            [option.name for option in self.live_system_options] or ["No system monitor found"]
        ))
        legacy_microphone = self.settings.live_device_id if self.settings.live_source == "microphone" else ""
        legacy_system = self.settings.live_device_id if self.settings.live_source == "system" else ""
        microphone_id = self.settings.live_microphone_device_id or legacy_microphone
        system_id = self.settings.live_system_device_id or legacy_system
        self.live_microphone_device.set_selected(next(
            (index for index, option in enumerate(self.live_microphone_options)
             if option.device_id == microphone_id), 0,
        ))
        self.live_system_device.set_selected(next(
            (index for index, option in enumerate(self.live_system_options)
             if option.device_id == system_id), 0,
        ))
        show_microphone = source in {"microphone", "both"}
        show_system = source in {"system", "both"}
        self.live_microphone_label.set_visible(show_microphone)
        self.live_microphone_device.set_visible(show_microphone)
        self.live_system_label.set_visible(show_system)
        self.live_system_device.set_visible(show_system)
        self.live_microphone_device.set_sensitive(bool(self.live_microphone_options))
        self.live_system_device.set_sensitive(bool(self.live_system_options))

    def _set_live_level(self, level: float) -> bool:
        self.live_level.set_value(level)
        db = 20 * math.log10(level) if level > 0 else float("-inf")
        self.live_db_label.set_label(f"{db:.1f} dBFS" if db != float("-inf") else "-- dBFS")
        self.live_db_label.remove_css_class("error")
        self.live_db_label.remove_css_class("warning")
        if level >= 0.98:
            self.live_clip_until = time.monotonic() + 1
        if time.monotonic() < self.live_clip_until:
            self.live_db_label.add_css_class("error")
        elif db > -12:
            self.live_db_label.add_css_class("warning")
        return False

    def _live_state(self, state: str) -> bool:
        self.live_status.set_label(state)
        if state == "Stopped" and self.live_session:
            self.live_last_frames = self.live_session.frames_sent
            self.live_last_bytes = self.live_session.bytes_sent
            self._append_live_diagnostic(
                f"Transport stopped; sent={self.live_last_frames} frames, "
                f"bytes={self.live_last_bytes}, events={self.live_event_count}"
            )
            self.live_session = None
            self.live_button.set_label("Start Listening")
            self.live_button.set_icon_name("media-record-symbolic")
            self.live_button.set_sensitive(True)
            self.floating_caption_message = (
                "Live session stopped — open Zoom API diagnostics"
            )
            self._render_floating_caption()
        return False

    def _show_caption_window(self, _button: Gtk.Button) -> None:
        if self.floating_window:
            self.floating_window.present()
            return
        window = Gtk.Window(title="Z Scribe Live Caption", transient_for=self)
        window.set_default_size(760, 250)
        window.set_size_request(360, 150)
        window.set_decorated(True)
        window.set_modal(False)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        root.add_css_class("floating-caption")
        root.set_margin_top(12)
        root.set_margin_bottom(12)
        root.set_margin_start(18)
        root.set_margin_end(18)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        size_label = Gtk.Label(label="Text size", xalign=0)
        size_label.add_css_class("dim-label")
        controls.append(size_label)
        self.floating_caption_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 14.0, 96.0, 1.0
        )
        self.floating_caption_scale.set_value(self.settings.live_caption_text_size)
        self.floating_caption_scale.set_digits(0)
        self.floating_caption_scale.set_draw_value(False)
        self.floating_caption_scale.set_hexpand(True)
        self.floating_caption_scale.set_tooltip_text(
            "Adjust the floating caption font size"
        )
        self.floating_caption_scale.connect(
            "value-changed", self._floating_caption_text_size_changed
        )
        controls.append(self.floating_caption_scale)
        self.floating_caption_size_value = Gtk.Label(xalign=1)
        self.floating_caption_size_value.set_width_chars(5)
        controls.append(self.floating_caption_size_value)
        root.append(controls)

        caption_scroll = Gtk.ScrolledWindow()
        caption_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        caption_scroll.set_vexpand(True)
        self.floating_caption_stack = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=6
        )
        self.floating_caption_stack.set_valign(Gtk.Align.START)
        self.floating_caption_stack.set_hexpand(True)
        caption_scroll.set_child(self.floating_caption_stack)
        root.append(caption_scroll)
        window.set_child(root)
        window.connect("close-request", self._caption_closed)
        self.floating_window = window
        self.floating_caption_message = None
        self._render_floating_caption()
        window.present()

    def _floating_caption_text_size_changed(self, scale: Gtk.Scale) -> None:
        self.settings.live_caption_text_size = min(
            max(float(scale.get_value()), 14.0), 96.0
        )
        if hasattr(self, "floating_caption_size_value"):
            self.floating_caption_size_value.set_label(
                f"{self.settings.live_caption_text_size:.0f} pt"
            )
        self.store.save_settings(self.settings)
        self._render_floating_caption()

    @staticmethod
    def _caption_markup(value: str, size: float, *, bold: bool = False) -> str:
        escaped = GLib.markup_escape_text(value)
        weight = " weight=\"bold\"" if bold else ""
        return (
            f"<span size=\"{int(round(size * Pango.SCALE))}\"{weight}>"
            f"{escaped}</span>"
        )

    def _render_floating_caption(self) -> None:
        stack = getattr(self, "floating_caption_stack", None)
        if stack is None:
            return
        while child := stack.get_first_child():
            stack.remove(child)
        size = self.settings.live_caption_text_size
        if self.floating_caption_message:
            label = Gtk.Label(xalign=0, wrap=True, justify=Gtk.Justification.LEFT)
            label.set_halign(Gtk.Align.FILL)
            label.set_hexpand(True)
            label.set_markup(self._caption_markup(self.floating_caption_message, size))
            stack.append(label)
            if hasattr(self, "floating_caption_size_value"):
                self.floating_caption_size_value.set_label(f"{size:.0f} pt")
            return

        entries = floating_caption_segments(self.live_final, self.live_interim)
        if not entries:
            entries = ["Waiting for speech…"]
        for entry in entries:
            source, separator, translation = entry.partition("\n")
            markup = self._caption_markup(source, size, bold=True)
            if separator and translation.strip():
                markup += "\n" + self._caption_markup(
                    translation.strip(), max(12.0, size * 0.77)
                )
            label = Gtk.Label(xalign=0, wrap=True, justify=Gtk.Justification.LEFT)
            label.set_halign(Gtk.Align.FILL)
            label.set_hexpand(True)
            label.set_markup(markup)
            stack.append(label)
        if hasattr(self, "floating_caption_size_value"):
            self.floating_caption_size_value.set_label(f"{size:.0f} pt")

    def _caption_closed(self, _window: Gtk.Window) -> bool:
        self.floating_window = None
        return False

    def _show_about(self, _button: Gtk.Button) -> None:
        about = Adw.AboutWindow(
            transient_for=self,
            application_name="Z Scribe for Linux",
            application_icon="com.tanchunsiong.ZScribeLinux",
            version=__version__,
            developer_name="Z Scribe Linux contributors",
            website="https://github.com/tanchunsiong/zoom-ai-services-linux-video-management-client",
            license_type=Gtk.License.MIT_X11,
        )
        about.present()

    def do_close_request(self) -> bool:
        self.cancel_event.set()
        self.live_summary_cancel.set()
        if self.live_session:
            self.live_session.stop()
        self.store.save_jobs(self.jobs)
        return False


class ZScribeApplication(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self) -> None:
        window = self.get_active_window()
        if not window:
            window = ZScribeWindow(self)
        window.present()

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        css = Gtk.CssProvider()
        css.load_from_data(b"""
            .caption-overlay { background: #111; color: white; padding: 12px; font-weight: 600; }
            .floating-caption { background: #111; color: white; padding: 18px; }
            .floating-caption-entry { color: white; }
            .live-transcript { font-size: 18px; padding: 18px; }
            .success { color: #2ec27e; }
            .error { color: #e01b24; }
            .accent { color: @accent_color; }
        """)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )


def main() -> int:
    # Keep the Wayland app_id/X11 WM_CLASS aligned with the desktop filename so
    # GNOME associates the window with its installed icon instead of a generic
    # executable/gear icon when launched through the Python console script.
    GLib.set_prgname(APP_ID)
    GLib.set_application_name("Z Scribe")
    return ZScribeApplication().run(None)


if __name__ == "__main__":
    raise SystemExit(main())
