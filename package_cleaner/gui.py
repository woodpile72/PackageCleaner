# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""Native GTK presentation. Package operations live in review_service and core modules."""
import os
import time
from . import __version__
from .core.operations import CancellationToken, OperationCancelled, operation
from concurrent.futures import ThreadPoolExecutor
from .core.review_service import ReviewService
from .core.cleanup import CleanupSession, Proposal
from .core.recommendations import CATEGORIES, inactivity_assessment, describe
from .display import local_date, installation_label
from .gui_icons import themed_image, decorate_button, icon_name, application_pixbuf, ICON_SIZES
from .core.footprint import LARGE_DESCRIPTION
from .core.classification import FILTERS, filter_components
from .core.providers import filter_sources
from .core.sorting import DEFAULTS, sort_options, sort_applications, observed_activity


def readable_size(value):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if value < 1024 or unit == 'TB':
            return f'{value:.1f} {unit}'
        value /= 1024


def last_use_label(item):
    """Only application activity can supply this date, never file access."""
    from datetime import datetime
    observation = observed_activity(item)
    if observation is None or observation.weak:
        return 'Last use: Unknown'
    observed = datetime.fromtimestamp(observation.timestamp).astimezone()
    return 'Last use: ' + observed.strftime('%b %d, %Y at %I:%M %p %Z')


def run(preferences=None):
    if os.geteuid() == 0:
        raise ValueError('Run the graphical application as your normal user, not as root.')
    try:
        import gi
        gi.require_version('Gtk', '3.0')
        from gi.repository import Gtk
    except ImportError as exc:
        raise RuntimeError('GTK 3 requires the system python3-gi and gir1.2-gtk-3.0 packages. Nothing was installed.') from exc
    from .core.preferences import Preferences
    service = ReviewService(Preferences(preferences))
    app = Gtk.Application(application_id='org.packagecleaner.Review')
    app.connect('activate', lambda application: Window(application, service).present())
    return app.run([])


# Lazy GTK imports keep CLI and service tests usable without a graphical session.
def Window(application, service):
    import gi
    gi.require_version('Gtk', '3.0')
    from gi.repository import Gtk, GLib, Gio, Pango

    class CategoryList(Gtk.ListBox):
        """Native, focusable navigation; IDs keep existing search routing intact."""
        def get_active_id(self):
            row = self.get_selected_row()
            return row.category_id if row else None

        def set_active_id(self, identifier):
            for row in self.get_children():
                if row.category_id == identifier:
                    self.select_row(row)
                    return

    class ReviewWindow(Gtk.ApplicationWindow):
        def __init__(self):
            super().__init__(application=application, title='Package Cleaner')
            self.set_icon_list([application_pixbuf(size) for size in ICON_SIZES])
            self.set_default_size(1100, 720)
            self.set_size_request(820, 520)
            self.service = service
            self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='package-cleaner-review')
            self.closed = False
            self.busy = False
            self.executing = False
            self.background_hold = False
            self.cleanup = None
            self.outcomes = []
            self.session_error = ''
            self.active_dialog = None
            self.items = []
            self.category_items = []
            self.component_page = 0
            self.sort_choices = {}
            self.order_positions = {}
            self.configuring_sort = False
            self.connect('delete-event', self.request_close)
            self.connect('destroy', self.close)
            self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
            self.paned.set_wide_handle(True)
            self.paned.set_can_focus(True)
            self.paned.get_accessible().set_name('Category sidebar divider')
            self.add(self.paned)
            sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            sidebar.set_size_request(220, -1)
            self.category = CategoryList(selection_mode=Gtk.SelectionMode.SINGLE)
            self.category.get_accessible().set_name('Cleanup categories')
            for key, name in CATEGORIES.items():
                if key == 'search':
                    continue
                row = Gtk.ListBoxRow()
                row.category_id = key
                label = Gtk.Label(label=name, xalign=0, wrap=True, width_chars=1,
                                  max_width_chars=24)
                label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
                category_content = Gtk.Box(spacing=8, margin=12)
                category_icon = themed_image(key)
                if category_icon is not None:
                    category_content.pack_start(category_icon, False, False, 0)
                category_content.pack_start(label, True, True, 0)
                row.add(category_content)
                self.category.add(row)
            self.category.set_active_id('large')
            category_scroll = Gtk.ScrolledWindow()
            category_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            category_scroll.add(self.category)
            sidebar.pack_start(category_scroll, True, True, 0)
            sidebar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)
            self.about_button = Gtk.Button(label='About', margin=8)
            decorate_button(self.about_button, 'about')
            self.about_button.connect('clicked', self.show_about)
            sidebar.pack_end(self.about_button, False, False, 0)
            self.paned.pack1(sidebar, resize=False, shrink=False)
            root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=12)
            root.set_size_request(600, -1)
            self.content_stack = Gtk.Stack()
            self.content_stack.add_named(root, 'review')
            self.about_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin=24)
            self.about_page.set_halign(Gtk.Align.CENTER)
            self.about_page.set_valign(Gtk.Align.CENTER)
            self.about_page.pack_start(Gtk.Image.new_from_pixbuf(application_pixbuf(96)), False, False, 0)
            about_name = Gtk.Label(xalign=0.5)
            about_name.set_markup('<big><b>Package Cleaner</b></big>')
            self.about_page.pack_start(about_name, False, False, 0)
            self.about_page.pack_start(Gtk.Label(label='© 2026 Erick Woods', xalign=0.5), False, False, 0)
            self.about_page.pack_start(Gtk.Label(label='Version ' + __version__, xalign=0.5), False, False, 0)
            self.content_stack.add_named(self.about_page, 'about')
            self.paned.pack2(self.content_stack, resize=True, shrink=False)
            self.paned.set_position(220)
            self.controls = Gtk.Box(spacing=12)
            title = self.heading = Gtk.Label(xalign=0, wrap=True, width_chars=1, max_width_chars=36)
            title.set_markup('<big><b>Review your installed applications</b></big>')
            self.controls.pack_start(title, True, True, 0)
            self.search = Gtk.SearchEntry(placeholder_text='Search applications', width_chars=20)
            self.search.set_max_width_chars(20)
            self.search.set_icon_from_icon_name(Gtk.EntryIconPosition.PRIMARY, icon_name(Gtk.IconTheme.get_default(), 'search'))
            self.search.set_valign(Gtk.Align.START)
            self.search.get_accessible().set_name('Search installed applications')
            self.search.connect('activate', self.search_now)
            self.search.connect('changed', self.search_now)
            self.controls.pack_end(self.search, False, False, 0)
            self.source = Gtk.ComboBoxText()
            for key, label in [('all','All sources'),('apt','APT'),('flatpak','Flatpak')]:
                self.source.append(key,label)
            self.source.set_active_id('all')
            self.source.set_valign(Gtk.Align.START)
            self.source.get_accessible().set_name('Filter software sources')
            self.source.connect('changed', self.source_changed)
            self.controls.pack_end(self.source, False, False, 0)
            self.all_items = []
            root.pack_start(self.controls, False, False, 0)
            self.category.connect('row-selected', self.category_selected)
            self.category.connect('row-activated', self.category_activated)
            status_row = Gtk.Box(spacing=8)
            self.status = Gtk.Label(xalign=0, wrap=True, width_chars=1)
            status_row.pack_start(self.status, True, True, 0)
            self.refresh = Gtk.Button(label='Refresh')
            decorate_button(self.refresh, 'refresh')
            self.refresh.connect('clicked', lambda _: self.scan())
            sorting = Gtk.Box(spacing=6)
            sorting.pack_start(Gtk.Label(label='Sort by'), False, False, 0)
            self.sort_by = Gtk.ComboBoxText()
            self.sort_by.get_accessible().set_name('Sort applications by')
            self.sort_by.set_popup_fixed_width(False)
            for cell in self.sort_by.get_cells():
                cell.set_property('ellipsize', Pango.EllipsizeMode.END)
                cell.set_property('max-width-chars', 22)
            self.sort_by.connect('changed', self.sort_changed)
            sorting.pack_start(self.sort_by, False, False, 0)
            sorting.pack_start(self.refresh, False, False, 0)
            status_row.pack_end(sorting, False, False, 0)
            root.pack_start(status_row, False, False, 0)
            self.component_controls = Gtk.Box(spacing=6)
            self.component_controls.pack_start(Gtk.Label(label='Show'), False, False, 0)
            self.component_filter = Gtk.ComboBoxText()
            for key,label in FILTERS.items():
                self.component_filter.append(key,label)
            self.component_filter.set_active_id('reviewable')
            self.component_filter.get_accessible().set_name('System component classification filter')
            self.component_filter.connect('changed', self.source_changed)
            self.component_controls.pack_start(self.component_filter, False, False, 0)
            root.pack_start(self.component_controls, False, False, 0)
            self.progress_area = Gtk.Box(spacing=8)
            self.progress_area.set_halign(Gtk.Align.START)
            root.pack_start(self.progress_area, False, False, 0)
            self.cancel_button = Gtk.Button(label='Cancel')
            decorate_button(self.cancel_button, 'cancel')
            self.cancel_button.set_tooltip_text('Cancel current check')
            self.cancel_button.set_sensitive(False)
            self.cancel_button.connect('clicked', lambda _: self.cancellation.cancel())
            self.spinner = Gtk.Spinner()
            self.progress_area.pack_start(self.spinner, False, False, 0)
            self.progress_area.pack_start(self.cancel_button, False, False, 0)
            scroll = Gtk.ScrolledWindow()
            self.application_scroll = scroll
            scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            self.rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
            self.rows.set_sort_func(lambda a, b: self.order_positions.get(a.package_id, 0) - self.order_positions.get(b.package_id, 0))
            scroll.add(self.rows)
            root.pack_start(scroll, True, True, 0)
            self.component_paging = Gtk.Box(spacing=8)
            self.component_previous = Gtk.Button(label='Previous')
            self.component_next = Gtk.Button(label='Next')
            self.component_page_label = Gtk.Label(xalign=0)
            self.component_previous.connect('clicked', lambda _: self.component_page_changed(-1))
            self.component_next.connect('clicked', lambda _: self.component_page_changed(1))
            self.component_paging.pack_start(self.component_previous,False,False,0)
            self.component_paging.pack_start(self.component_page_label,False,False,0)
            self.component_paging.pack_start(self.component_next,False,False,0)
            root.pack_start(self.component_paging,False,False,0)
            footer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            queue_actions = Gtk.Box(spacing=8)
            self.selection_label = Gtk.Label(xalign=0, wrap=True, width_chars=1, max_width_chars=25)
            footer.pack_start(self.selection_label, True, True, 0)
            self.preview_button = Gtk.Button(label='Review cleanup queue')
            self.preview_button.connect('clicked', lambda _: self.perform('Validating the removal queue…', service.validate, self.show_preview))
            queue_actions.pack_start(self.preview_button, False, False, 0)
            self.remove_button = Gtk.Button(label='Remove Selected Software')
            self.remove_button.set_tooltip_text('Validate and review the queue before confirming removal')
            self.remove_button.connect('clicked', self.start_cleanup)
            queue_actions.pack_start(self.remove_button, False, False, 0)
            self.clear_button = Gtk.Button(label='Clear queue')
            self.clear_button.connect('clicked', self.clear_selection)
            queue_actions.pack_start(self.clear_button, False, False, 0)
            footer.pack_start(queue_actions, False, False, 0)
            root.pack_start(footer, False, False, 0)
            self.footer = footer
            self.show_all()
            self.component_controls.set_no_show_all(True)
            self.component_controls.hide()
            self.component_paging.set_no_show_all(True)
            self.component_paging.hide()
            self.progress_area.set_no_show_all(True)
            self.progress_area.hide()
            self.cancel_button.hide()
            self.update_selection()
            self.scan()

        def show_about(self, *_):
            self.content_stack.set_visible_child_name('about')
            self.about_button.get_style_context().add_class('suggested-action')

        def show_review(self):
            self.content_stack.set_visible_child_name('review')
            self.about_button.get_style_context().remove_class('suggested-action')

        def category_selected(self, _, row):
            if row is not None:
                self.show_review()
                self.scan()

        def category_activated(self, _, row):
            # Clicking the already-selected category also leaves About, without
            # rebuilding the existing list or losing its search/filter state.
            if row is not None:
                self.show_review()

        def request_close(self, *_):
            if not self.executing:
                return False
            dialog = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.INFO,
                                       text='Removal is still in progress.')
            dialog.format_secondary_text('Verification must finish before closing. You can hide this window; it will reopen with the result. Completed removals are not rolled back.')
            dialog.add_button('Keep open', Gtk.ResponseType.CANCEL)
            dialog.add_button('Finish in background', Gtk.ResponseType.OK)
            response = dialog.run()
            dialog.destroy()
            if response == Gtk.ResponseType.OK:
                if not self.background_hold:
                    application.hold()
                    self.background_hold = True
                self.hide()
            return True

        def close(self, *_):
            self.closed = True
            if self.busy:
                self.cancellation.cancel()
            self.executor.shutdown(wait=False, cancel_futures=True)

        def update_selection(self):
            self.selection_label.set_text(f'{len(service.selected)} applications queued for removal')
            self.preview_button.set_sensitive(bool(service.selected) and not self.busy)
            self.clear_button.set_sensitive(bool(service.selected) and not self.busy)
            self.remove_button.set_sensitive(bool(service.selected) and not self.busy)

        def perform(self, message, work, done, *, execution=False, can_cancel=True):
            if self.busy:
                return
            self.busy = True
            self.executing = execution
            self.cancellation = CancellationToken()
            self.cancel_button.set_sensitive(can_cancel)
            self.progress_area.show()
            self.cancel_button.set_visible(can_cancel)
            self.cancel_button.set_tooltip_text('Request cancellation; completed removals cannot be rolled back' if execution else 'Cancel current check')
            self.category.set_sensitive(False)
            self.about_button.set_sensitive(False)
            self.refresh.set_sensitive(False)
            self.sort_by.set_sensitive(False)
            self.controls.set_sensitive(False)
            self.rows.set_sensitive(False)
            self.update_selection()
            self.spinner.start()
            self.status.set_text(message)
            last_progress = [0.0]
            def notify(event):
                now = time.monotonic()
                if now - last_progress[0] < 0.15:
                    return
                last_progress[0] = now
                titles = {'inventory': 'Reading installed applications', 'discovery': 'Finding applications',
                          'simulation': 'Validating the removal queue', 'application_metadata': 'Reading application information'}
                titles.update({'validation':'Checking the removal proposal', 'executing_apt':'APT: authenticating and removing software',
                               'executing_flatpak':'Flatpak: authenticating and removing software',
                               'flatpak_execution':'Flatpak: processing the approved removal'})
                text = titles.get(event.phase, 'Verifying results' if execution else 'Working') + '…'
                if event.total:
                    text += f' {event.completed} of {event.total}'
                def update():
                    if not self.closed and self.busy:
                        self.status.set_text(text)
                    return False
                GLib.idle_add(update)
            def background():
                with operation(notify, self.cancellation):
                    return work()
            future = self.executor.submit(background)
            def still_waiting():
                if self.closed or future.done():
                    return False
                if execution:
                    self.status.set_text('Still waiting for the package operation. Verification is pending; do not retry. You can finish in the background by closing this window.')
                return True
            waiting_source = GLib.timeout_add_seconds(60, still_waiting)
            def finish():
                if GLib.MainContext.default().find_source_by_id(waiting_source):
                    GLib.source_remove(waiting_source)
                if self.closed:
                    return False
                self.busy = False
                self.executing = False
                if self.background_hold:
                    self.present()
                    application.release()
                    self.background_hold = False
                self.cancel_button.set_sensitive(False)
                self.cancel_button.hide()
                self.progress_area.hide()
                self.category.set_sensitive(True)
                self.about_button.set_sensitive(True)
                self.refresh.set_sensitive(True)
                self.sort_by.set_sensitive(True)
                self.spinner.stop()
                self.controls.set_sensitive(True)
                self.rows.set_sensitive(True)
                self.update_selection()
                try:
                    value = future.result()
                except OperationCancelled:
                    if self.cleanup:
                        self.cleanup.cancel()
                    self.status.set_text('Check cancelled. Remaining removal choices are saved; review again before proceeding.')
                    if self.outcomes:
                        self.session_error = 'Further removal was cancelled. Previous completed removals were not rolled back.'
                        self.execution_results()
                except Exception as exc:
                    if self.cleanup:
                        self.cleanup.cancel()
                    self.status.set_text('Removal stopped; review the saved queue again.' if execution else 'This review could not be completed. Please refresh and try again.')
                    if self.outcomes:
                        self.session_error = 'Further removal stopped: ' + str(exc)
                        self.execution_results()
                    else:
                        self.message('Removal stopped' if execution else 'Review could not be completed', str(exc))
                else:
                    done(value)
                return False
            future.add_done_callback(lambda _: GLib.idle_add(finish))

        def view_category(self):
            return 'search' if self.search.get_text().strip() else self.category.get_active_id()

        def search_now(self, *_):
            if self.busy:
                return
            query = self.search.get_text().strip()
            self.all_items = service.search_cached(query).applications if query else list(self.category_items)
            self.source_changed()

        def scan(self):
            category = self.category.get_active_id()
            self.perform('Finding applications…', lambda: service.discover(category), self.discovered)

        def discovered(self, result):
            items, warnings = result
            self.category_items = list(service.component_items if self.category.get_active_id() == 'advanced' else items)
            self.search_now()
            if warnings:
                self.status.set_text(self.status.get_text() + ' Some information was unavailable.')
                self.message('Scan observations', '\n'.join(warnings))

        def source_changed(self, *_):
            self.component_page = 0
            category = self.view_category()
            self.component_controls.set_visible(category == 'advanced')
            matching = filter_components(self.all_items,self.component_filter.get_active_id()) if category == 'advanced' else self.all_items
            self.items = filter_sources(matching,self.source.get_active_id())
            self.configure_sort()
            self.render(self.items)
            count = len(self.items)
            category = self.view_category()
            self.heading.set_markup('<big><b>' + ('Search results' if category == 'search' else 'Review your installed applications') + '</b></big>')
            if category == 'search':
                summary = f'Search results · {count} found.'
            elif category == 'advanced':
                summary = f'{count} components · ' + FILTERS[self.component_filter.get_active_id()]
            elif category == 'large':
                summary = LARGE_DESCRIPTION + f' · {count} found.' if count else 'No applications meet the 100 MB threshold.'
            elif category == 'support' and not count:
                summary = 'No supporting packages can be safely removed.'
            else:
                summary = f'Found {count} applications to review.'
            self.status.set_text(summary)

        def configure_sort(self):
            options = sort_options(self.items)
            category = self.view_category()
            choice = self.sort_choices.get(category, DEFAULTS[category])
            if choice not in options:
                choice = DEFAULTS[category] if DEFAULTS[category] in options else 'name_asc'
            self.configuring_sort = True
            self.sort_by.remove_all()
            for key, label in options.items():
                self.sort_by.append(key, label)
            self.sort_by.set_active_id(choice)
            self.configuring_sort = False
            self.apply_sort(refresh_rows=False)

        def sort_changed(self, *_):
            if self.configuring_sort or not self.sort_by.get_active_id():
                return
            self.component_page = 0
            self.sort_choices[self.view_category()] = self.sort_by.get_active_id()
            self.apply_sort()

        def apply_sort(self, refresh_rows=True):
            choice = self.sort_by.get_active_id() or DEFAULTS[self.view_category()]
            self.items = sort_applications(self.items, choice)
            self.order_positions = {item.package: i for i, item in enumerate(self.items)}
            self.sort_by.set_tooltip_text(self.sort_by.get_active_text())
            # Reorder existing widgets: focused application/action and scrollable
            # row identity survive. No discovery, preference or queue calls.
            if self.view_category() == 'advanced' and refresh_rows:
                self.render(self.items)
            else:
                self.rows.invalidate_sort()

        def component_page_changed(self, delta):
            self.component_page = max(0,self.component_page + delta)
            self.render(self.items)
            self.application_scroll.get_vadjustment().set_value(0)

        def render(self, items):
            for child in self.rows.get_children():
                self.rows.remove(child)
            paging = self.view_category() == 'advanced' and len(items) > 100
            self.component_paging.set_visible(paging)
            if paging:
                self.component_page = min(self.component_page,(len(items)-1)//100)
                start = self.component_page * 100
                visible_items = items[start:start+100]
                self.component_page_label.set_text(f'{start+1}–{start+len(visible_items)} of {len(items)} components')
                self.component_previous.set_sensitive(start>0)
                self.component_next.set_sensitive(start+100<len(items))
            else:
                visible_items = items
            for item in visible_items:
                row = Gtk.Box(spacing=14, margin=12)
                icon = service.icons.get(item.package, 'application-x-executable')
                image = Gtk.Image.new_from_gicon(Gio.FileIcon.new(Gio.File.new_for_path(icon)), Gtk.IconSize.DIALOG) if os.path.isabs(icon) else Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.DIALOG)
                row.pack_start(image, False, False, 0)
                text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
                title = Gtk.Label(xalign=0, wrap=True, width_chars=1)
                title.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
                title.set_markup('<b>' + GLib.markup_escape_text(item.title) + '</b>')
                text.pack_start(title, False, False, 0)
                if item.package in service.selected:
                    text.pack_start(Gtk.Label(label='Queued for removal', xalign=0), False, False, 0)
                for content in (('Flatpak' if item.provider == 'flatpak' else 'APT') + ' · ' + readable_size(item.installed_bytes) + ' installed footprint · ' + last_use_label(item),):
                    label = Gtk.Label(label=content, xalign=0, wrap=True, width_chars=1, max_width_chars=65)
                    label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
                    if 'Last use: ' in content:
                        label.set_tooltip_text('Dates come from recorded application requests, which may be automated and may not be the latest activity. File access alone does not establish use. See Details for evidence.')
                    text.pack_start(label, False, False, 0)
                if self.view_category() == 'advanced' and item.component_assessment:
                    text.pack_start(Gtk.Label(label=item.component_assessment.classification.value.title(), xalign=0), False, False, 0)
                actions = Gtk.Box(spacing=8)
                for label, callback in (
                    ('Details', lambda _, i=item: self.details(i)),
                    ('Keep permanently', lambda _, i=item: self.perform('Saving your Keep choice…', lambda: service.keep(i.package), lambda value: self.after_keep(i))),
                    ('Queue Removal', lambda _, i=item: self.perform('Updating the removal queue…', lambda: service.select(i.package), lambda value: self.after_queue(i)))):
                    button = Gtk.Button(label=label)
                    decorate_button(button, {'Details': 'details', 'Keep permanently': 'keep', 'Queue Removal': 'queue'}[label])
                    if label == 'Queue Removal':
                        button.set_sensitive(not item.protected)
                        button.set_tooltip_text('Save your intention to remove this application. Review cleanup queue validates the proposed transaction; queuing never authorizes execution.')
                    button.connect('clicked', callback)
                    actions.pack_start(button, False, False, 0)
                text.pack_start(actions, False, False, 0)
                row.pack_start(text, True, True, 0)
                container = Gtk.ListBoxRow()
                container.package_id = item.package
                container.add(row)
                self.rows.add(container)
            self.rows.show_all()

        def after_queue(self, item):
            self.status.set_text(item.title + ': Queued for removal. Review cleanup queue to validate the proposal.')
            self.render(self.items)

        def after_keep(self, item):
            self.status.set_text(item.title + ' will no longer be suggested.')
            self.category_items = [i for i in self.category_items if i.package != item.package]
            self.all_items = [i for i in self.all_items if i.package != item.package]
            self.items = [i for i in self.items if i.package != item.package]
            self.configure_sort()
            self.render(self.items)

        def message(self, title, content):
            dialog = Gtk.Dialog(title=title, transient_for=self, modal=True)
            dialog.add_button('Close', Gtk.ResponseType.CLOSE)
            dialog.set_default_size(620, 350)
            scroll = Gtk.ScrolledWindow()
            label = Gtk.Label(label=content, xalign=0, yalign=0, wrap=True, selectable=True, margin=16)
            label.set_max_width_chars(75)
            scroll.add(label)
            dialog.get_content_area().pack_start(scroll, True, True, 0)
            dialog.show_all()
            dialog.run()
            dialog.destroy()

        def details(self, item):
            e = item.evidence
            lines = ([] if item.title.strip() == item.purpose.strip() else [item.title]) + [
                     'Package: ' + item.package, 'Description: ' + item.purpose,
                     'Installed application footprint: ' + readable_size(item.installed_bytes) + ' (not a removal estimate).',
                     last_use_label(item), *inactivity_assessment(e)['reasons'],
                     'Last observed file access: ' + local_date(e.latest_file_access),
                     'File access is not a record of application execution.']
            if item.provider == 'flatpak':
                p = service.packages[item.package]
                lines = [item.title, 'Application ID: '+p.base, 'Description: '+item.purpose,
                         'Source: Flatpak', 'Installation: '+installation_label(p.installation),
                         'Installation identifier: '+p.installation, 'Installed ref: '+p.ref,
                         'Installed size: '+readable_size(p.size)+' (shared runtime excluded).',
                         'Runtime: '+(p.runtime or 'Not recorded'), last_use_label(item),
                         'Application data is retained.']
            if item.component_assessment:
                a = item.component_assessment
                lines.append('Classification: '+a.classification.value.title()+'\n'+'\n'.join(a.reasons))
                if a.required_by:
                    lines.append('Related installed packages: '+', '.join(a.required_by[:8])+(' …' if len(a.required_by)>8 else ''))
                if a.manual_investigation:
                    lines.append('Investigation only. Manual installation flags will not be changed.')
            if item.protected:
                lines.append('Protected: '+ '; '.join(item.protected))
            if item.footprint_components:
                lines.append('Main package size: ' + readable_size(service.packages[item.package].size))
                lines.append('Attributed application components: ' + ', '.join(item.footprint_components[:8]) + (' …' if len(item.footprint_components) > 8 else ''))
                lines.append('Shared libraries and components used by other applications are not included in this footprint.')
            if e.operational:
                lines.append('Removing this application would stop its running software. Running does not establish use.')
            if e.activity:
                lines.append('Application request logs contain recent activity.')
            observation = observed_activity(item)
            if observation:
                lines.append('Activity-date sorting: ' + local_date(observation.recorded_date) + '\nSource: ' + observation.source)
                if observation.weak:
                    lines.append('This weak timestamp can reflect reads or backups, not execution.')
                else:
                    lines.append('Recorded requests may be automated; this is not a human last-use date.')
            self.message('Application details', '\n\n'.join(lines))

        def show_preview(self, preview):
            self.status.set_text('Removal blocked by safety checks.' if not preview.ready else 'Removal queue validated for review. Execution has not been authorized.')
            dialog = Gtk.Dialog(title='Removal queue', transient_for=self, modal=True)
            dialog.set_default_size(650, 520)
            dialog.add_button('Back to review', Gtk.ResponseType.CANCEL)
            if preview.ready:
                dialog.add_button('Remove Selected Software…', Gtk.ResponseType.ACCEPT)
            outer = Gtk.ScrolledWindow()
            outer.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            dialog.get_content_area().pack_start(outer, True, True, 0)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            outer.add(box)
            box.set_spacing(12)
            box.set_border_width(16)
            summary = ('Removal blocked by safety checks.' if not preview.ready else 'Validated removal preview — ready for review.')
            summary += '\n\n' + '\n'.join(preview.applications)
            summary += f'\n\nAdditional unused supporting packages: {preview.supporting_count}'
            summary += '\nRecoverable space: not validated.' if preview.plan.blocked else ('\nPotential recovery upper bound (shared storage may reduce this): ' if preview.plan.provider_plans else '\nEstimated recoverable application space: ') + readable_size(preview.recoverable_bytes)
            if preview.plan.baseline_selected_bytes:
                summary += '\nSeparately selected existing unused packages: ' + readable_size(preview.plan.baseline_selected_bytes)
            for note in preview.plan.observations:
                if 'currently running' in note:
                    summary += '\nWarning: '+note
            summary += '\n\nReview each transaction before confirming removal.'
            label = Gtk.Label(label=summary, xalign=0, wrap=True)
            summary_row = Gtk.Box(spacing=8)
            if not preview.ready or preview.warnings or 'Warning: ' in summary:
                warning_icon = themed_image('warning')
                if warning_icon is not None:
                    warning_icon.set_valign(Gtk.Align.START)
                    summary_row.pack_start(warning_icon, False, False, 0)
            summary_row.pack_start(label, True, True, 0)
            box.pack_start(summary_row, False, False, 0)
            expander = Gtk.Expander(label='Details: complete package list and safety findings')
            scroll = Gtk.ScrolledWindow()
            scroll.set_min_content_height(160)
            details = '\n'.join(preview.plan.blocked + preview.warnings)
            for part in preview.plan.provider_plans:
                details += '\n\n' + part.provider.upper() + ' — ' + part.installation + '\n' + '\n'.join(part.observations + part.blocked)
            details += '\n\nExplicitly queued applications:\n' + '\n'.join(preview.plan.requested)
            details += '\n\nComplete proposed removals:\n' + '\n'.join(preview.plan.removed)
            details += '\n\nExisting orphan savings (separate): ' + readable_size(preview.plan.baseline_selected_bytes)
            technical = Gtk.Label(label=details, selectable=True, xalign=0, yalign=0, wrap=True)
            scroll.add(technical)
            expander.add(scroll)
            box.pack_start(expander, True, True, 0)
            for name in sorted(service.selected):
                button = Gtk.Button(label='Remove from queue: ' + (describe(service.packages[name])[0] if name in service.packages else 'Application no longer installed'))
                def remove(_, n=name):
                    dialog.response(Gtk.ResponseType.CANCEL)
                    self.perform('Updating the removal queue…', lambda: service.unstage(n), lambda value: self.queue_updated('Removal queue updated. Review it again to validate the changes.'))
                button.connect('clicked', remove)
                box.pack_start(button, False, False, 0)
            dialog.show_all()
            response = dialog.run()
            dialog.destroy()
            if response == Gtk.ResponseType.ACCEPT:
                self.start_cleanup()

        def start_cleanup(self, *_):
            if self.busy or not service.selected:
                return
            self.cleanup = CleanupSession(service, graphical=True)
            self.outcomes = []
            self.session_error = ''
            self.perform('Validating the removal queue…', self.cleanup.begin, self.confirm_cleanup)

        def confirmation_dialog(self, proposal):
            plan = proposal.plan
            dialog = Gtk.Dialog(title='Remove selected software?', transient_for=self, modal=True)
            dialog.set_default_size(620, 440)
            dialog.add_button('Cancel', Gtk.ResponseType.CANCEL)
            remove = dialog.add_button('Remove', Gtk.ResponseType.ACCEPT)
            remove.get_style_context().add_class('destructive-action')
            remove.set_tooltip_text('Uninstall the software listed in this transaction')
            dialog.set_default_response(Gtk.ResponseType.CANCEL)
            scroll = Gtk.ScrolledWindow()
            scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            dialog.get_content_area().pack_start(scroll, True, True, 0)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin=16)
            scroll.add(box)
            lines = [plan.provider.upper() + ' · ' + installation_label(plan.installation)]
            if proposal.total > 1:
                lines.append(f'Transaction {proposal.index} of {proposal.total}. Each is confirmed separately; completed removals cannot be rolled back.')
            if proposal.changed:
                lines.append('The proposal changed. Please review this updated list before approving.')
            lines += ['\n' + '\n'.join(describe(proposal.packages[n])[0] for n in plan.requested)]
            lines.append(f'Additional supporting packages: {len(plan.newly_orphaned)}')
            if plan.dependent_removals:
                lines.append('Other affected software: ' + ', '.join(describe(proposal.packages[n])[0] for n in plan.dependent_removals))
            from .core.recommendations import application_savings
            amount = sum(plan.savings.values()) if plan.provider == 'flatpak' else application_savings(plan) + plan.baseline_selected_bytes
            lines.append(('Potential recovery upper bound: ' if plan.provider == 'flatpak' else 'Estimated recoverable space: ') + readable_size(amount))
            lines.append('Application data will be kept.' if plan.provider == 'flatpak' else 'User files and package configuration are retained; uninstall scripts may perform package-specific cleanup.')
            label = Gtk.Label(label='\n\n'.join(lines), xalign=0, wrap=True, max_width_chars=65)
            box.pack_start(label, False, False, 0)
            details = Gtk.Expander(label='Details')
            text = 'Installation identifier: ' + plan.installation + '\n\nComplete removals:\n' + '\n'.join(plan.removed)
            text += '\n\n' + '\n'.join(plan.observations)
            technical = Gtk.Label(label=text, selectable=True, xalign=0, wrap=True, max_width_chars=65)
            details.add(technical)
            box.pack_start(details, False, False, 0)
            dialog.show_all()
            return dialog

        def confirm_cleanup(self, proposal):
            self.status.set_text('Review the removal proposal.')
            dialog = self.confirmation_dialog(proposal)
            self.active_dialog = dialog
            response = dialog.run()
            self.active_dialog = None
            dialog.destroy()
            if response != Gtk.ResponseType.ACCEPT:
                self.cleanup.cancel()
                if self.outcomes:
                    self.execution_results()
                else:
                    self.status.set_text('Removal cancelled. Your queue is saved.')
                return
            provider = proposal.plan.provider
            self.perform(provider.upper() + ': checking approval and preparing removal…',
                         lambda: self.cleanup.confirm(proposal.token, accepted=True), self.cleanup_finished,
                         execution=True, can_cancel=provider == 'flatpak')

        def cleanup_finished(self, value):
            if isinstance(value, Proposal):
                self.confirm_cleanup(value)
                return
            self.outcomes.append(value)
            self.update_selection()
            if value.more:
                self.perform('Preparing the next independent transaction…', self.cleanup.propose, self.confirm_cleanup)
            else:
                self.execution_results()

        def execution_results(self):
            outcomes = list(self.outcomes)
            all_complete = all(o.result.completed for o in outcomes)
            unknown = any(o.result.get('status') == 'unknown' or not o.result.get('helper_verified') for o in outcomes)
            title = 'Cleanup complete' if all_complete and not service.selected else 'Removal results'
            if unknown:
                title = 'Verification incomplete — reconciliation required'
            lines = [self.session_error] if self.session_error else []
            technical = []
            for o in outcomes:
                r = o.result
                labels = {'completed':'Completed and verified', 'partial':'Partially completed — changes were not rolled back',
                          'cancelled':'Cancelled before changes', 'failed':'Failed without changes',
                          'rejected':'Stopped before removal', 'unknown':'Result uncertain; reconciliation required'}
                lines.append(o.plan.provider.upper() + ' · ' + installation_label(o.plan.installation) + '\n' + labels.get(r.get('status'),'Result uncertain'))
                removed = r.get('removed', [])
                lines.append(('Removed: ' if r.get('helper_verified') else 'Observed absent; verification incomplete: ') + (', '.join(describe(o.packages[n])[0] for n in removed if n in o.packages and n in o.plan.requested) or 'No applications verified removed'))
                lines.append(f'Supporting packages removed: {len(set(removed)-set(o.plan.requested))}')
                if removed:
                    lines.append('Removed installed-size estimate: ' + readable_size(sum(o.packages[n].size for n in removed if n in o.packages)) + '; actual disk recovery was not measured.')
                if r.get('remaining'):
                    lines.append('Still installed: ' + ', '.join(describe(o.packages[n])[0] for n in r['remaining'] if n in o.packages))
                if r.get('error'):
                    lines.append(str(r['error']).splitlines()[0][:180])
                technical.append('Log: ' + r.get('log_directory','Unavailable') + '\n' + str(r.get('error') or '') + '\nObserved removals:\n' + '\n'.join(removed))
            lines.append(f'{len(service.selected)} applications remain in the removal queue.')
            dialog = Gtk.Dialog(title=title, transient_for=self, modal=True)
            dialog.set_default_size(640, 460)
            dialog.add_button('Close', Gtk.ResponseType.CLOSE)
            scroll = Gtk.ScrolledWindow()
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=12,margin=16)
            scroll.add(box);dialog.get_content_area().pack_start(scroll,True,True,0)
            box.pack_start(Gtk.Label(label='\n\n'.join(lines),xalign=0,wrap=True,max_width_chars=65),False,False,0)
            details = Gtk.Expander(label='Details and execution logs')
            details.add(Gtk.Label(label='\n\n'.join(technical),xalign=0,wrap=True,selectable=True,max_width_chars=65))
            box.pack_start(details,False,False,0)
            self.active_dialog = dialog
            dialog.show_all();dialog.run();dialog.destroy()
            self.active_dialog = None
            self.outcomes = []
            self.session_error = ''
            self.scan()

        def queue_updated(self, message):
            self.status.set_text(message)
            self.render(self.items)

        def clear_selection(self, *_):
            dialog = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                       buttons=Gtk.ButtonsType.OK_CANCEL, text='Clear your removal queue?')
            response = dialog.run()
            dialog.destroy()
            if response == Gtk.ResponseType.OK:
                self.perform('Clearing the removal queue…', service.unstage, lambda value: self.queue_updated('Removal queue cleared.'))

    return ReviewWindow()


if __name__ == "__main__":
    raise SystemExit(run())
