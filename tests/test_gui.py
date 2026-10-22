# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""Read-only GUI/controller tests. No privileged helper is ever called."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from package_cleaner.review_service import ReviewService
from package_cleaner.preferences import Preferences
from package_cleaner.inventory import Package
from package_cleaner.removal import Plan, Baseline
from package_cleaner.recommendations import Recommendation, Evidence


_GTK_TEST_APPLICATION = None


def gtk_test_application():
    global _GTK_TEST_APPLICATION
    if _GTK_TEST_APPLICATION is None:
        from gi.repository import Gtk, Gio
        _GTK_TEST_APPLICATION = Gtk.Application(application_id='org.packagecleaner.Test', flags=Gio.ApplicationFlags.NON_UNIQUE)
        _GTK_TEST_APPLICATION.register(None)
    return _GTK_TEST_APPLICATION


class ReviewServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.prefs = Preferences(Path(self.temp.name) / 'preferences.json')
        self.service = ReviewService(self.prefs, Path(self.temp.name) / 'selection.json')
        self.packages = {'app:amd64': Package('app:amd64', 'app', '1', False, 100, 'An application', 'graphics', application=True)}
        self.plan = Plan(['app:amd64'], ['app:amd64'], [], [], [], [], [],
                         {'selected':100, 'newly_orphaned':0, 'already_orphaned':0}, [], '', Baseline([], ''))
        self.item = Recommendation('app:amd64', 'Application', 'Draw pictures', '', Evidence(), 0, [], installed_bytes=100)
        self.inv = patch('package_cleaner.review_service.inventory', return_value=(self.packages, []))
        self.inv.start(); self.addCleanup(self.inv.stop)

    def test_categories_search_and_filter_are_core_discovery(self):
        with patch('package_cleaner.review_service.recommend', return_value=[self.item]) as recommend, patch('package_cleaner.review_service.package_paths',return_value=[]):
            for category in ('large', 'potential', 'servers', 'advanced', 'search'):
                items, _ = self.service.discover(category, 'drawing')
                self.assertEqual(items, [self.item])
                self.assertEqual(recommend.call_args.kwargs['category'], category)
                self.assertEqual(recommend.call_args.kwargs['query'], 'drawing')

    def test_keep_shared_and_persistent(self):
        external = Preferences(self.prefs.path)
        external.keep('other:amd64')
        self.service.keep('app:amd64')
        self.assertEqual(Preferences(self.prefs.path).kept, {'other:amd64', 'apt:system/app:amd64'})
        with self.assertRaisesRegex(ValueError, 'Keep'):
            self.service.stage('app:amd64')

    def test_stage_exact_selection_preview_and_restart(self):
        with patch('package_cleaner.review_service.simulate', return_value=self.plan) as simulate:
            preview = self.service.stage('app:amd64')
        self.assertEqual(simulate.call_args.args[0], ['app:amd64'])
        self.assertTrue(preview.ready)
        self.assertEqual(preview.recoverable_bytes, 100)
        restored = ReviewService(self.prefs, self.service.selection_path)
        self.assertEqual(restored.selected, {'app:amd64'})
        self.assertIsNone(restored.preview)

    def test_blocked_plan_is_not_ready(self):
        self.plan.blocked = ['Protected system software']
        with patch('package_cleaner.review_service.simulate', return_value=self.plan):
            self.assertFalse(self.service.stage('app:amd64').ready)

    def test_preview_failure_clears_previous_validation(self):
        self.service.preview = object()
        with patch('package_cleaner.review_service.simulate', side_effect=ValueError('state changed')):
            with self.assertRaises(ValueError): self.service.stage('app:amd64')
        self.assertIsNone(self.service.preview)

    def test_cancel_and_remove_selection(self):
        self.service.selected = {'app:amd64', 'other:amd64'}
        self.service.unstage('app:amd64')
        self.assertEqual(self.service.selected, {'other:amd64'})
        self.service.unstage()
        self.assertFalse(self.service.selected)

    def test_execution_cannot_be_enabled_by_confirmation(self):
        with patch('package_cleaner.execution.launch') as launch:
            for text in ('', 'REMOVE', 'yes'):
                with self.assertRaisesRegex(ValueError, 'not enabled'):
                    from package_cleaner.core import execute_confirmed
                    execute_confirmed({}, text)
        launch.assert_not_called()

    def test_support_category_excludes_protected_orphans(self):
        self.packages['app:amd64'].orphan = True
        self.packages['app:amd64'].protected = ['System']
        with patch('package_cleaner.review_service.package_paths', return_value=[]):
            items, _ = self.service.discover('support')
        self.assertEqual(items, [])

    def test_existing_orphan_savings_are_separate(self):
        self.plan.baseline_selected_bytes = 40
        with patch('package_cleaner.review_service.simulate', return_value=self.plan):
            self.assertEqual(self.service.stage('app:amd64').recoverable_bytes, 60)

    def test_protected_selection_uses_real_core_safeguards(self):
        self.packages['app:amd64'].protected = ['Protected system component']
        with patch('package_cleaner.removal.package_state', return_value='stable'), patch(
                'package_cleaner.removal.apt_simulation', side_effect=[([], ''), (['app:amd64'], ''), (['app:amd64'], ''), (['app:amd64'], '')]):
            preview = self.service.stage('app:amd64')
        self.assertFalse(preview.ready)
        self.assertTrue(preview.plan.blocked)

    def test_combined_selection_only_contains_individual_choices(self):
        self.service.selected = {'other:amd64'}
        self.packages['other:amd64'] = Package('other:amd64','other','1',False,50,'Other','graphics',application=True)
        with patch('package_cleaner.review_service.simulate', return_value=self.plan) as simulate:
            self.service.stage('app:amd64')
        self.assertEqual(simulate.call_args.args[0], ['app:amd64','other:amd64'])

    def test_cli_preference_object_picks_up_gui_changes(self):
        cli = Preferences(self.prefs.path)
        self.service.keep('app:amd64')
        cli.reload()
        self.assertIn('apt:system/app:amd64', cli.kept)
        cli.keep('other:amd64')
        self.service.refresh_preferences()
        self.assertEqual(self.service.preferences.kept, {'apt:system/app:amd64','other:amd64'})

    def test_queue_is_persisted_intention_not_validation(self):
        self.service.preview = object()
        with patch('package_cleaner.review_service.simulate') as simulation, patch('package_cleaner.core.execution.launch') as launch:
            self.service.select('app:amd64')
            simulation.assert_not_called()
            launch.assert_not_called()
        self.assertIsNone(self.service.preview)
        restored = ReviewService(self.prefs, self.service.selection_path)
        self.assertEqual(restored.selected, {'app:amd64'})
        self.assertIsNone(restored.preview)

    def test_gui_command_is_lazy_and_never_forwards_execution_opt_in(self):
        from package_cleaner.__main__ import main
        with patch('sys.argv', ['package-cleaner','--enable-removal','gui']), patch('package_cleaner.gui.run', return_value=0) as run:
            self.assertEqual(main(), 0)
        run.assert_called_once_with(None)


@unittest.skipUnless(os.environ.get('PACKAGE_CLEANER_GTK_TESTS') == '1', 'Opt-in display test')
class GtkTests(ReviewServiceTests):
    def test_component_filters_cached_search_and_protection(self):
        import gi
        gi.require_version('Gtk','3.0')
        from gi.repository import Gtk, GLib, Gio
        from package_cleaner.gui import Window
        if not Gtk.init_check()[0]: self.skipTest('No display')
        app=gtk_test_application()
        tool=Package('tool','tool','1',False,1,'Independent tool','admin',public_commands=['/usr/bin/tool'],file_metadata_available=True)
        uncertain=Package('support','support','1',False,1,'Library','libs',file_metadata_available=True)
        protected=Package('protected','protected','1',True,1,'Critical component','libs',protected=['policy'],dependencies=['required'])
        required=Package('required','required','1',True,1,'Dependency','libs')
        self.packages.update({p.name:p for p in (tool,uncertain,protected,required)})
        def drain(w):
            while w.busy: GLib.MainContext.default().iteration(True)
        with patch('package_cleaner.review_service.recommend',return_value=[self.item]),patch('package_cleaner.review_service.package_paths',return_value=[]),patch('package_cleaner.review_service.simulate') as simulation:
            window=Window(app,self.service)
            try:
                drain(window)
                self.assertFalse(window.component_controls.get_visible())
                window.category.set_active_id('advanced');drain(window)
                self.assertEqual([i.package for i in window.items],['tool'])
                self.assertEqual(window.sort_by.get_active_id(),'review_priority')
                self.assertTrue(window.component_controls.get_visible())
                self.service.select('tool')
                saved=self.service.selection_path.read_bytes()
                with patch.object(self.service,'inventory_provider',side_effect=AssertionError('Filter must not scan')):
                    window.component_filter.set_active_id('investigate')
                    self.assertIn('support',[i.package for i in window.items])
                    window.component_filter.set_active_id('required')
                    self.assertEqual([i.package for i in window.items],['required'])
                    window.component_filter.set_active_id('all')
                    row=next(r for r in window.rows.get_children() if r.package_id=='protected')
                    actions=row.get_child().get_children()[1].get_children()[-1].get_children()
                    self.assertFalse(next(b for b in actions if b.get_label()=='Queue Removal').get_sensitive())
                    window.search.set_text('tool')
                    self.assertFalse(window.component_controls.get_visible())
                    window.search.set_text('')
                    self.assertEqual(window.component_filter.get_active_id(),'all')
                    window.component_filter.set_active_id('reviewable')
                    self.assertEqual([i.package for i in window.items],['tool'])
                simulation.assert_not_called()
                self.assertEqual(saved,self.service.selection_path.read_bytes())
                from dataclasses import replace
                template=window.items[0]
                window.all_items=[replace(template,package=f'tool-{n}',title=f'Tool {n:03}',installed_bytes=n) for n in range(205)]
                window.source_changed()
                self.assertTrue(window.component_paging.get_visible())
                seen=[]
                for count in (100,100,5):
                    self.assertEqual(len(window.rows.get_children()),count)
                    seen.extend(r.package_id for r in window.rows.get_children())
                    if window.component_next.get_sensitive():window.component_next.clicked()
                self.assertEqual(len(set(seen)),205)
                self.assertFalse(window.component_next.get_sensitive())
                window.sort_by.set_active_id('size_desc')
                self.assertEqual(window.rows.get_children()[0].package_id,'tool-204')
                self.assertEqual(window.component_page,0)
                self.assertEqual(saved,self.service.selection_path.read_bytes())
            finally:
                window.destroy()

    def test_native_widgets_navigation_and_preview(self):
        import gi
        gi.require_version('Gtk','3.0')
        from gi.repository import Gtk, GLib, Gio
        from package_cleaner.gui import Window
        if not Gtk.init_check()[0]: self.skipTest('No display')
        app = gtk_test_application()
        def drain(window):
            deadline = time.monotonic() + 3
            while window.busy and time.monotonic() < deadline:
                GLib.MainContext.default().iteration(True)
            self.assertFalse(window.busy)
        with patch('package_cleaner.review_service.recommend', return_value=[self.item]) as discover, patch('package_cleaner.review_service.package_paths', return_value=[]), patch('package_cleaner.review_service.simulate', return_value=self.plan) as simulation, patch.object(Gtk.Dialog,'run',return_value=Gtk.ResponseType.CANCEL):
            window = Window(app, self.service)
            try:
                drain(window)
                self.assertEqual(len(window.rows.get_children()),1)
                self.assertEqual(window.selection_label.get_text(), '0 applications queued for removal')
                before = discover.call_count
                window.source.set_active_id('flatpak')
                self.assertEqual(len(window.rows.get_children()),0)
                window.source.set_active_id('apt')
                self.assertEqual(len(window.rows.get_children()),1)
                window.source.set_active_id('all')
                self.assertEqual(discover.call_count,before)
                self.assertIsInstance(window.paned, Gtk.Paned)
                self.assertIsInstance(window.category, Gtk.ListBox)
                self.assertEqual(len(window.category.get_children()), 5)
                self.assertEqual(window.category.get_active_id(), 'large')
                self.assertFalse(window.progress_area.get_visible())
                self.assertFalse(window.cancel_button.get_visible())
                self.assertEqual(window.cancel_button.get_label(), 'Cancel')
                self.assertTrue(window.category.child_focus(Gtk.DirectionType.TAB_FORWARD))
                window.search.grab_focus()
                def settle():
                    deadline = time.monotonic() + .15
                    while time.monotonic() < deadline:
                        while GLib.MainContext.default().pending():
                            GLib.MainContext.default().iteration(False)
                        time.sleep(.005)
                window.present()
                settle()
                window.search.grab_focus()
                settle()
                self.assertIs(window.get_focus(), window.search)
                window.resize(1100,720)
                window.paned.set_position(220)
                settle()
                window.resize(1400,900)
                settle()
                self.assertEqual(window.paned.get_position(),220)
                window.paned.grab_focus()
                settle()
                self.assertIs(window.get_focus(), window.paned)
                before = window.paned.get_position()
                window.paned.emit('move-handle', Gtk.ScrollType.STEP_RIGHT)
                settle()
                self.assertGreater(window.paned.get_position(),before)
                for width, height, sidebar in ((820,520,220),(1100,720,220),(1400,900,300)):
                    window.resize(width,height)
                    window.paned.set_position(sidebar)
                    settle()
                    self.assertEqual(window.paned.get_position(),sidebar)
                    self.assertTrue(all(row.get_mapped() for row in window.category.get_children()))
                    horizontal = window.application_scroll.get_hadjustment()
                    self.assertLessEqual(horizontal.get_upper(), horizontal.get_page_size())
                    self.assertTrue(window.footer.get_mapped())
                    self.assertGreater(window.application_scroll.get_allocated_height(),200)
                    self.assertLess(window.search.get_allocated_width(),260)
                    self.assertEqual(window.refresh.get_parent().get_valign(), Gtk.Align.CENTER)
                    list_bottom = window.application_scroll.translate_coordinates(window,0,window.application_scroll.get_allocated_height())[1]
                    footer_top = window.footer.translate_coordinates(window,0,0)[1]
                    self.assertLessEqual(list_bottom,footer_top)
                from copy import deepcopy
                other = deepcopy(self.item)
                other.package, other.title, other.installed_bytes = 'other:amd64', 'Zulu', 1000
                service_package = deepcopy(self.packages['app:amd64'])
                service_package.name = 'other:amd64'
                self.service.packages['other:amd64'] = service_package
                window.discovered(([self.item, other], []))
                self.assertEqual(window.sort_by.get_active_id(), 'size_desc')
                self.assertEqual([r.package_id for r in window.rows.get_children()], ['other:amd64','app:amd64'])
                row_identity = {r.package_id:r for r in window.rows.get_children()}
                self.service.select('app:amd64')
                saved_queue = self.service.selection_path.read_bytes()
                search_before = window.search.get_text()
                calls_before = discover.call_count
                focused_action = row_identity['app:amd64'].get_child().get_children()[1].get_children()[-1].get_children()[0]
                focused_action.grab_focus()
                self.assertIs(window.get_focus(), focused_action)
                with patch('package_cleaner.core.review_service.inventory') as inventory:
                    window.sort_by.set_active_id('name_asc')
                    self.assertEqual([r.package_id for r in window.rows.get_children()], ['app:amd64','other:amd64'])
                    window.sort_by.set_active_id('name_desc')
                    inventory.assert_not_called()
                self.assertEqual(discover.call_count,calls_before)
                self.assertIs(window.get_focus(),focused_action)
                simulation.assert_not_called()
                self.assertEqual(window.search.get_text(), search_before)
                self.assertEqual(self.service.selection_path.read_bytes(),saved_queue)
                self.assertEqual({r.package_id:r for r in window.rows.get_children()},row_identity)
                self.service.unstage()
                window.category.set_active_id('servers')
                drain(window)
                self.assertEqual(discover.call_args.kwargs['category'],'servers')
                self.assertEqual(window.sort_by.get_active_id(),'name_asc')
                window.category.set_active_id('large')
                drain(window)
                self.assertEqual(window.sort_by.get_active_id(),'name_desc')
                calls_before = discover.call_count
                window.search.set_text('app:amd64')
                window.search_now()
                drain(window)
                self.assertEqual(discover.call_count,calls_before)
                self.assertEqual(window.category.get_active_id(),'large')
                self.assertEqual(window.heading.get_text(),'Search Results')
                self.assertIn('1 found', window.status.get_text())
                window.search.set_text('')
                self.assertEqual([i.package for i in window.items],['app:amd64'])
                self.assertEqual(window.sort_by.get_active_id(),'name_desc')
                protected = Package('libudev1:i386','libudev1','1',True,1,'Device library','libs',protected=['system'])
                self.service.packages[protected.name] = protected
                window.search.set_text(protected.name)
                self.assertTrue(window.items[0].protected)
                protected_actions = window.rows.get_children()[0].get_child().get_children()[1].get_children()[-1].get_children()
                self.assertFalse(next(b for b in protected_actions if b.get_label() == 'Queue Removal').get_sensitive())
                window.search.set_text('')
                self.assertEqual(discover.call_count,calls_before)
                window.details(self.item)
                def descendants(widget):
                    yield widget
                    if isinstance(widget, Gtk.Container):
                        for child in widget.get_children():
                            yield from descendants(child)
                queue = next(w for w in descendants(window) if isinstance(w, Gtk.Button) and w.get_label() == 'Queue Removal')
                self.assertEqual(queue.get_accessible().get_name(), 'Queue Removal')
                self.assertIn('never authorizes execution', queue.get_tooltip_text())
                queue.clicked()
                drain(window)
                simulation.assert_not_called()
                self.assertIn('Queued for removal', window.status.get_text())
                self.assertEqual(window.preview_button.get_label(), 'Review Cleanup Queue')
                self.assertEqual(window.remove_button.get_label(), 'Remove Selected Software')
                self.assertEqual(window.clear_button.get_label(), 'Clear Queue')
                labels = [w.get_text() for w in descendants(window) if isinstance(w, Gtk.Label)]
                for title in ('Large Applications', 'Potentially Unused Applications',
                              'Servers and Development Tools', 'Unneeded Supporting Packages',
                              'System Tools and Background Components', 'Sort by'):
                    self.assertIn(title, labels)
                self.assertEqual(window.selection_label.get_text(), '1 application queued for removal')
                self.assertEqual(window.search.get_placeholder_text(), 'Search applications')
                window.preview_button.clicked()
                drain(window)
                simulation.assert_called_once()

                self.assertEqual(self.service.selected,{'app:amd64'})
                window.clear_selection()  # Cancel preserves staged intentions.
                self.assertEqual(self.service.selected,{'app:amd64'})
                self.plan.blocked=['Protected software']
                window.show_preview(self.service.validate())
                self.assertIn('blocked',window.status.get_text())
                from package_cleaner.core.operations import checkpoint
                def until_cancelled():
                    while True:
                        checkpoint()
                        time.sleep(0.01)
                window.perform('Checking', until_cancelled, lambda _: self.fail('Cancelled operation completed'))
                self.assertTrue(window.cancel_button.get_visible())
                self.assertTrue(window.progress_area.get_visible())
                window.cancel_button.clicked()
                drain(window)
                self.assertIn('cancelled', window.status.get_text())
                self.assertFalse(window.progress_area.get_visible())
                self.assertFalse(window.cancel_button.get_visible())
            finally:
                window.destroy()
