# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""Dialog construction only; authorization remains in the shared core."""
from .display import installation_label
from .core.recommendations import describe
from .gui_formatting import readable_size


class Dialogs:
    def message(self, title, content):
        from gi.repository import Gtk
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


    def confirmation_dialog(self, proposal):
        from gi.repository import Gtk
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
