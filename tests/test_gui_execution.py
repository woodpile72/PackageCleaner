# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""Native GTK execution workflow with fake privileged/package backends only."""
import os
import threading
import time
import unittest
from unittest.mock import patch


@unittest.skipUnless(os.environ.get('PACKAGE_CLEANER_GTK_TESTS')=='1','Native GTK tests opt in')
class GtkExecutionTests(unittest.TestCase):
    def setUp(self):
        import gi
        gi.require_version('Gtk','3.0')
        from gi.repository import Gtk,GLib
        from test_gui import gtk_test_application
        from test_cleanup import CleanupTests
        from package_cleaner.gui import Window
        self.Gtk,self.GLib=Gtk,GLib
        self.fixture=CleanupTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.window=Window(gtk_test_application(),self.fixture.service)
        self.addCleanup(self.window.destroy)
        self.settle()
    def settle(self,predicate=None):
        deadline=time.monotonic()+10
        predicate=predicate or (lambda:not self.window.busy)
        while not predicate() and time.monotonic()<deadline:
            while self.GLib.MainContext.default().pending():self.GLib.MainContext.default().iteration(False)
            time.sleep(.005)
        self.assertTrue(predicate(),'GTK operation timed out')
    def auto_dialog(self,callback):
        def check():
            if self.window.active_dialog:
                callback(self.window.active_dialog)
                return False
            return True
        return self.GLib.timeout_add(10,check)
    def test_button_states_and_cancelled_confirmation(self):
        self.assertTrue(self.window.remove_button.get_sensitive())
        seen=[]
        def cancel(dialog):
            seen.append(dialog)
            self.assertEqual(dialog.get_title(),'Remove Selected Software?')
            self.assertTrue(dialog.get_widget_for_response(self.Gtk.ResponseType.ACCEPT).get_style_context().has_class('destructive-action'))
            dialog.response(self.Gtk.ResponseType.CANCEL)
        self.auto_dialog(cancel);self.window.start_cleanup();self.settle()
        self.fixture.execute_mock.assert_not_called()
        self.assertEqual(len(seen),1)
        self.fixture.service.unstage();self.window.update_selection()
        self.assertFalse(self.window.remove_button.get_sensitive())
    def test_success_uses_worker_and_result_dialog_then_refresh(self):
        worker_threads=[];seen=[]
        original=self.fixture.success
        def success(*args,**kw):
            worker_threads.append(threading.get_ident())
            return original(*args,**kw)
        self.fixture.execute_mock.side_effect=success
        def respond():
            dialog=self.window.active_dialog
            if dialog:
                seen.append(dialog.get_title())
                dialog.response(self.Gtk.ResponseType.ACCEPT if dialog.get_title()=='Remove Selected Software?' else self.Gtk.ResponseType.CLOSE)
            return True
        source=self.GLib.timeout_add(10,respond)
        self.window.start_cleanup()
        self.assertFalse(self.window.remove_button.get_sensitive())
        self.settle(lambda:'Cleanup Complete' in seen and not self.window.busy)
        self.GLib.source_remove(source)
        self.assertEqual(self.fixture.execute_mock.call_count,1)
        self.assertTrue(all(t!=threading.get_ident() for t in worker_threads))
        self.assertFalse(self.fixture.service.selected)
    def test_close_during_execution_keeps_verification_alive(self):
        self.window.executing=True
        with patch.object(self.Gtk.MessageDialog,'run',return_value=self.Gtk.ResponseType.CANCEL):
            self.assertTrue(self.window.request_close())
        self.assertFalse(self.window.closed)
        self.window.executing=False
        self.assertFalse(self.window.request_close())
    def test_busy_prevents_duplicate_clicks(self):
        import threading
        gate=threading.Event()
        self.window.perform('Checking',lambda:gate.wait(2),lambda _:None)
        self.assertFalse(self.window.remove_button.get_sensitive())
        self.window.start_cleanup();self.window.start_cleanup()
        self.assertIsNone(self.window.cleanup)
        gate.set();self.settle()
    def test_changed_transaction_dialog_requires_another_click(self):
        from package_cleaner.core.cleanup import Proposal
        proposal=self.fixture.session.begin()
        changed=Proposal(proposal.token,proposal.plan,proposal.packages,1,1,True)
        self.window.cleanup=self.fixture.session
        self.auto_dialog(lambda d:d.response(self.Gtk.ResponseType.CANCEL))
        self.window.confirm_cleanup(changed)
        self.fixture.execute_mock.assert_not_called()
    def test_about_navigation_preserves_review_state(self):
        window=self.window
        window.search.set_text('Application')
        window.source.set_active_id('apt')
        window.component_filter.set_active_id('all')
        before=(set(self.fixture.service.selected),set(self.fixture.service.preferences.kept))
        rows=list(window.rows.get_children())
        with patch.object(window,'scan') as scan:
            window.about_button.clicked()
            self.assertEqual(window.get_title(),'Package Cleaner')
            self.assertEqual(window.content_stack.get_visible_child_name(),'about')
            labels=[child.get_text() for child in window.about_page.get_children() if isinstance(child,self.Gtk.Label)]
            self.assertEqual(labels,['Package Cleaner','© 2026 Erick Woods','Version 0.1.4'])
            self.assertEqual(window.about_page.get_halign(),self.Gtk.Align.CENTER)
            self.assertEqual(window.about_page.get_valign(),self.Gtk.Align.CENTER)
            self.assertTrue(all(child.get_xalign()==0.5 for child in window.about_page.get_children() if isinstance(child,self.Gtk.Label)))
            window.category.emit('row-activated',window.category.get_selected_row())
            self.assertEqual(window.content_stack.get_visible_child_name(),'review')
            scan.assert_not_called()
            window.about_button.clicked()
            window.category.set_active_id('servers')
            self.assertEqual(window.content_stack.get_visible_child_name(),'review')
            scan.assert_called_once()
        self.assertEqual(window.search.get_text(),'Application')
        self.assertEqual(window.source.get_active_id(),'apt')
        self.assertEqual(window.component_filter.get_active_id(),'all')
        self.assertEqual(rows,list(window.rows.get_children()))
        self.assertEqual(before,(set(self.fixture.service.selected),set(self.fixture.service.preferences.kept)))
        self.fixture.execute_mock.assert_not_called()

    def test_about_stays_at_sidebar_bottom_at_multiple_sizes(self):
        window=self.window
        window.about_button.clicked()
        for width,height in [(820,520),(1100,720),(1280,800)]:
            window.resize(width,height)
            until=time.monotonic()+.12
            while time.monotonic()<until:
                while self.GLib.MainContext.default().pending():self.GLib.MainContext.default().iteration(False)
                time.sleep(.005)
            button=window.about_button
            self.assertTrue(button.get_mapped())
            position=button.translate_coordinates(window,0,0)
            self.assertGreater(position[1],window.get_allocated_height()-80)
            self.assertLessEqual(position[1]+button.get_allocated_height(),window.get_allocated_height())
            self.assertTrue(button.get_can_focus())
            self.assertEqual(window.content_stack.get_visible_child_name(),'about')
    def test_disabled_preview_shows_reason_and_no_remove_action(self):
        from package_cleaner.core.review_service import Preview
        plan=self.fixture.plan
        plan.execution_disabled=True;plan.observations=['Required validation signal unavailable']
        preview=Preview(plan,['Example'],0,0,[])
        self.fixture.service.preview=preview
        seen=[]
        def inspect(dialog):
            seen.append(True)
            self.assertIsNone(dialog.get_widget_for_response(self.Gtk.ResponseType.ACCEPT))
            self.assertIn('Required validation signal unavailable',self.window.status.get_text())
            return self.Gtk.ResponseType.CANCEL
        with patch.object(self.Gtk.Dialog,'run',inspect):self.window.show_preview(preview)
        self.assertTrue(seen)
        self.assertFalse(self.window.remove_button.get_sensitive())
        self.assertTrue(self.window.preview_button.get_sensitive())
        self.fixture.execute_mock.assert_not_called()
