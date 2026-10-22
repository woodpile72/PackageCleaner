# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""Structured review operations shared with terminal discovery and APT safety code.

Selections are intentions only. Persisted selections are never approvals.
"""
from dataclasses import dataclass
from pathlib import Path
import json
import os
import tempfile
import configparser
from .inspection import read_inspection_text
from .recommendations import package_paths
from .providers import inventory, preview as simulate, effective_kept
from .recommendations import recommend, Recommendation, Evidence, describe, application_savings
from .preferences import Preferences, xdg_directory
from .removal import Plan
from .operations import domain_operation, CleanerError, checkpoint


@dataclass
class Preview:
    plan: Plan
    applications: list[str]
    supporting_count: int
    recoverable_bytes: int
    warnings: list[str]

    @property
    def ready(self):
        return bool(self.plan.removed) and not self.plan.blocked


@dataclass
class Discovery:
    applications: list[Recommendation]
    warnings: list[str]

    def __iter__(self):
        yield self.applications
        yield self.warnings


class ReviewService:
    def __init__(self, preferences=None, selection_path=None, *, inventory_provider=None, simulation_provider=None):
        self.preferences = preferences or Preferences()
        self.inventory_provider = inventory_provider or (lambda: inventory())
        self.simulation_provider = simulation_provider or (lambda *args: simulate(*args))
        self.selection_path = None if selection_path is False else (Path(selection_path) if selection_path else xdg_directory('XDG_STATE_HOME', '.local/state') / 'package-cleaner/cleanup-selection.json')
        self.selected = set()
        if self.selection_path and self.selection_path.exists():
            data = json.loads(self.selection_path.read_text())
            if not isinstance(data, list) or not all(isinstance(n, str) for n in data):
                raise CleanerError('Saved cleanup selection is invalid.')
            self.selected = {n.removeprefix('apt:system/') for n in data}
        self.packages = {}
        self.preview = None
        self.icons = {}
        self.cached_evidence = {}
        self.component_assessments = {}
        self.component_items = []

    def refresh_preferences(self):
        self.preferences = Preferences(self.preferences.path)

    def save(self):
        if self.selection_path is None:
            return
        self.selection_path.parent.mkdir(parents=True, exist_ok=True)
        name = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', dir=self.selection_path.parent, delete=False) as stream:
                name = stream.name
                json.dump(sorted(n if n.startswith('flatpak:') else 'apt:system/'+n for n in self.selected), stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.selection_path)
        finally:
            if name and os.path.exists(name):
                os.unlink(name)

    @domain_operation
    def discover(self, category, query=''):
        self.refresh_preferences()
        self.packages, warnings = self.inventory_provider()
        from .classification import classify_components, component_recommendations
        self.component_assessments = classify_components(self.packages)
        self.component_items = component_recommendations(self.packages,self.preferences.kept,'all',self.component_assessments)
        items = recommend(self.packages, self.preferences.kept, category=category, query=query)
        self.icons = {}
        for item in items:
            item.component_assessment = self.component_assessments.get(item.package)
        self.cached_evidence = {item.package:item.evidence for item in items}
        for item in items:
            checkpoint('application_metadata')
            if item.provider == 'flatpak':
                self.icons[item.package] = self.packages[item.package].base
                continue
            for path in package_paths(self.packages[item.package]):
                if not path.endswith('.desktop'):
                    continue
                try:
                    desktop = configparser.ConfigParser(interpolation=None, strict=False)
                    desktop.read_string(read_inspection_text(Path(path)))
                    self.icons[item.package] = desktop.get('Desktop Entry', 'Icon', fallback='application-x-executable')
                    break
                except (OSError, configparser.Error):
                    continue
        return Discovery(items, warnings)

    @domain_operation
    def search_cached(self, query):
        """Metadata-only global search. Never refresh inventory or resolve transactions.

        Exact identifiers also expose supporting/protected packages for inspection.
        Keep choices are not changed; explicit search can inspect kept applications.
        """
        from .inventory import application_root
        from .footprint import application_footprint
        query = query.strip().casefold()
        if not query:
            return Discovery([], [])
        items = []
        for p in self.packages.values():
            exact = query in {p.name.casefold(), p.base.casefold(), p.identity.casefold(), p.ref.casefold()}
            title, description, _ = describe(p)
            if not exact and (not application_root(p) or query not in
                             ' '.join((title,description,p.summary,p.name,p.base)).casefold()):
                continue
            footprint = application_footprint(p.name,self.packages) if p.provider == 'apt' else None
            items.append(Recommendation(p.name,title,description,'',self.cached_evidence.get(p.name, Evidence(running=p.running)),0,[],
                installed_bytes=footprint.installed_bytes if footprint else p.size,
                footprint_components=list(footprint.components) if footprint else [],
                provider=p.provider,identity=p.identity,installation=p.installation,
                protected=list(p.protected),component_assessment=self.component_assessments.get(p.name)))
        return Discovery(items, [])

    @domain_operation
    def keep(self, name):
        self.refresh_preferences()
        self.preferences.keep(name if name.startswith('flatpak:') else 'apt:system/'+name)
        self.selected.discard(name)
        self.preview = None
        self.save()

    @domain_operation
    def unstage(self, name=None):
        if name is None:
            self.selected.clear()
        else:
            self.selected.discard(name)
        self.preview = None
        self.save()

    @domain_operation
    def select(self, name, *, replace_keep=False):
        self.refresh_preferences()
        kept = effective_kept(self.packages, self.preferences.kept)
        if name in kept and replace_keep:
            self.preferences.reset([name, name if name.startswith('flatpak:') else 'apt:system/'+name])
        if name in effective_kept(self.packages, self.preferences.kept):
            raise CleanerError('This application is saved with Keep. Reset that preference before selecting removal.')
        if name in self.packages and self.packages[name].protected:
            raise CleanerError('This package is protected and cannot be queued for removal.')
        # A blocked selection remains visible so users can remove it or inspect it.
        self.selected.add(name)
        self.preview = None
        self.save()

    @domain_operation
    def stage(self, name):
        self.select(name)
        return self.validate()

    def affected_applications(self, plan):
        self.refresh_preferences()
        return [row['name'] for row in plan.unselected_manual
                if not self.packages[row['name']].protected and row['name'] not in effective_kept(self.packages,self.preferences.kept)]

    @domain_operation
    def validate(self):
        self.preview = None
        self.refresh_preferences()
        if not self.selected:
            raise CleanerError('Choose an application to review first.')
        self.packages, warnings = self.inventory_provider()
        plan = self.simulation_provider(sorted(self.selected), self.packages, self.preferences.kept)
        self.preview = Preview(plan, [describe(self.packages[n])[0] for n in plan.requested],
                               len(plan.newly_orphaned), 0 if plan.blocked else application_savings(plan), warnings + plan.observations)
        return self.preview



    def reconcile_removed(self, names):
        """Remove only verified absent selections, preserving other saved intentions."""
        if self.selection_path and self.selection_path.exists():
            data=json.loads(self.selection_path.read_text())
            if not isinstance(data,list) or not all(isinstance(n,str) for n in data):
                raise CleanerError('Saved removal queue is invalid; it was not overwritten.')
            self.selected={n.removeprefix('apt:system/') for n in data}
        self.selected.difference_update(names)
        self.preview=None
        self.save()
