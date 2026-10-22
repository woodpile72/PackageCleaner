# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""Bounded, explainable recommendations. Lack of history never establishes non-use."""
from .classification import ComponentAssessment
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
import hashlib
import math
import json
import os
import time
from pathlib import Path

from .operations import checkpoint
from .footprint import application_footprint, LARGE_MIN, size_class
from .analysis import command, review_units
from .inventory import application_root
from .removal import simulate
from .service_activity import inspect_server
from .state import PackageStateChanged
from .inspection import open_inspection, read_inspection_text

# Editorial descriptions describe purpose, never assert anything about local use.
DESCRIPTIONS = {
    'apache2': ('Apache web server', 'Hosts websites on this computer.', 'Keep it if you host a website or test websites locally.'),
    'build-essential': ('Software development tools', 'Provides tools for building software from source.', 'Keep these if you compile programs or work on software projects.'),
    'firefox': ('Firefox', 'Browses the web.', 'Keep it if you use this browser or need its saved settings.'),
    'chromium': ('Chromium', 'Browses the web.', 'Keep it if you use this browser or test websites with it.'),
    'thunderbird': ('Thunderbird', 'Reads and manages email.', 'Keep it if you use it for email or calendars.'),
    'vlc': ('VLC media player', 'Plays music and videos.', 'Keep it if you use it to play media files.'),
    'gimp': ('GIMP image editor', 'Edits photographs and other images.', 'Keep it if you edit images with it.'),
    'code': ('Visual Studio Code', 'Edits code and text for software projects.', 'Keep it if you use it for coding or text editing.'),
    'zoom': ('Zoom', 'Joins video meetings.', 'Keep it if you attend meetings using Zoom.'),
}


def describe(package):
    if package.base in DESCRIPTIONS:
        return DESCRIPTIONS[package.base]
    if package.stack == 'MariaDB / MySQL':
        if '-server' in package.base:
            return ('Database server', 'Stores information for websites and other applications.',
                    'Keep it if another application stores its information here. Removing software does not assess or delete your databases.')
        if '-client' in package.base:
            return ('Database connection tools', 'Connects to databases on this computer or another computer.',
                    'Keep these if you work with databases; these tools alone do not mean you host a database server.')
    if package.stack == 'PHP':
        return ('PHP website tools', 'Runs code used by some websites and web development tools.',
                'Keep these if you build or run websites that use PHP.')
    name = package.display_name or package.summary or 'Unidentified software'
    purpose = package.summary or 'A reliable plain-language description is not available.'
    return (name, purpose, 'Keep it if you recognize it as something you use. If you are unsure, choose Skip or Details.')


def package_paths(package):
    if package.provider != 'apt':
        return []
    for key in (package.name, package.base):
        try:
            return read_inspection_text(Path(f'/var/lib/dpkg/info/{key}.list')).splitlines()
        except OSError:
            continue
    return []


def running_executables():
    found = set()
    # No arguments or environment variables are collected. Permissions limit coverage.
    for entry in Path('/proc').glob('[0-9]*/exe'):
        try:
            found.add(os.readlink(entry).removesuffix(' (deleted)'))
        except OSError:
            pass
    return found


@dataclass
class Evidence:
    running: bool = False
    recent_accesses: int = 0
    executable_count: int = 0
    old_accesses: int = 0
    latest_file_access: str | None = None
    services: list[dict] = field(default_factory=list)
    modified_configuration: int = 0
    unreadable_configuration: int = 0
    recorded_age_days: int | None = None
    limitations: list[str] = field(default_factory=list)
    server_observations: dict | None = None

    @property
    def activity(self):
        # Meaningful activity reader evidence only. Lifecycle logs are not usage.
        return bool(self.server_observations and self.server_observations['application_activity'].get('recent_request_records', 0))

    @property
    def operational(self):
        return self.running or any(s.get('active') in ('active', 'activating', 'reloading') for s in self.services)


def collect_evidence(package, running, paths=None, now=None, inspect_services=True):
    if package.provider == 'flatpak':
        return Evidence(running=package.running)
    now = now or datetime.now()
    evidence = Evidence()
    if package.installed_at:
        evidence.recorded_age_days = max(0, (now - datetime.fromisoformat(package.installed_at)).days)
    paths = package_paths(package) if paths is None else paths
    units = set()
    for raw in paths:
        path = Path(raw)
        if raw in running:
            evidence.running = True
        if raw.startswith(('/usr/bin/', '/usr/sbin/', '/bin/', '/sbin/', '/usr/libexec/')):
            try:
                stat = path.stat()
                if path.is_file() and stat.st_mode & 0o111:
                    evidence.executable_count += 1
                    if str(path.resolve()) in running or raw in running:
                        evidence.running = True
                    if 946684800 <= stat.st_atime <= now.timestamp():
                        stamp = datetime.fromtimestamp(stat.st_atime, timezone.utc).isoformat()
                        evidence.latest_file_access = max(evidence.latest_file_access or '', stamp)
                        if stat.st_atime < now.timestamp() - 365 * 86400:
                            evidence.old_accesses += 1
                    if stat.st_atime >= now.timestamp() - 90 * 86400:
                        evidence.recent_accesses += 1
            except OSError:
                pass
        if path.suffix == '.service' and ('/systemd/system/' in raw or '/systemd/user/' in raw):
            units.add((path.name, '/systemd/user/' in raw))
    # Config contents are compared to packaged hashes locally and never printed or saved.
    for path, expected in package.conffiles:
        try:
            if Path(path).stat().st_size > 1024 * 1024:
                evidence.unreadable_configuration += 1
                continue
            with open_inspection(path, 'rb') as stream:
                value = hashlib.md5(stream.read(1024 * 1024 + 1)).hexdigest()
            evidence.modified_configuration += value != expected
        except OSError:
            evidence.unreadable_configuration += 1
    if inspect_services:
        for unit, user in sorted(units)[:4]:
            scope = ['--user'] if user else []
            code, out, err = command(['systemctl', *scope, 'show', '--no-pager',
                                     '--property=LoadState,ActiveState,UnitFileState', unit], timeout=2)
            fields = dict(line.split('=', 1) for line in out.splitlines() if '=' in line)
            loaded = code == 0 and fields.get('LoadState') == 'loaded'
            row = {'unit': unit, 'active': fields.get('ActiveState') if loaded else None,
                   'enabled': fields.get('UnitFileState') if loaded else None,
                   'status_accessible': loaded}
            code, out, err = command(['journalctl', *scope, '--no-pager', '--quiet',
                                     '--user-unit' if user else '--unit', unit,
                                     '-n', '1', '-o', 'json'], timeout=2)
            row['journal_entry'] = False
            row['last_journal_event'] = None
            if code == 0 and out.strip():
                try:
                    record = json.loads(out.strip().splitlines()[-1])
                    stamp = int(record['__REALTIME_TIMESTAMP']) / 1_000_000
                    if 0 <= stamp <= now.timestamp():
                        row['last_journal_event'] = datetime.fromtimestamp(stamp, timezone.utc).isoformat()
                        row['journal_entry'] = stamp >= now.timestamp() - 90 * 86400
                except (ValueError, KeyError, TypeError, OverflowError, AttributeError):
                    evidence.limitations.append('A journal timestamp could not be interpreted; no usage date inferred.')
            row['journal_accessible'] = code == 0
            evidence.services.append(row)
        if len(units) > 4:
            evidence.limitations.append('Only the first four packaged service units were inspected.')
        evidence.server_observations = inspect_server(package, now=now)
    elif units:
        evidence.limitations.append('Service checks not performed in the initial shortlist pass.')
    evidence.limitations.append('Access times can reflect reads or backups, not execution. Old or missing access times do not establish non-use.')
    evidence.limitations.append('Process visibility, journal permissions and retention are incomplete. No retained journal event does not establish inactivity.')
    return evidence


def evidence_summary(evidence):
    if evidence.activity:
        return 'Access logs record recent handled HTTP requests. These may include automated probes; they do not prove human use.'
    if evidence.running:
        return 'A program is running now. Running does not establish that anyone is using it.'
    if any(s.get('active') in ('active', 'activating', 'reloading') for s in evidence.services):
        return 'A background service is running now. Running does not establish that anyone is using it.'
    if any(s.get('journal_entry') for s in evidence.services):
        return 'Recent service lifecycle records are available. Startup and reload events are not application-usage evidence.'
    if evidence.modified_configuration:
        return 'Its settings differ from the installed defaults. Someone may have set it up for a purpose; check before removing it.'
    if evidence.recent_accesses:
        return 'Some of its program files were accessed within the last 90 days. This could be use, a backup, or another file read.'
    inactive = [s for s in evidence.services if s.get('active') == 'inactive' and s.get('enabled') in ('disabled', 'masked')]
    if inactive:
        return 'A background service is stopped and is not set to start normally. This is only its current state; past use is unknown.'
    return 'There is not enough reliable history to tell whether you use this application. Missing history does not mean it is unused.'


def priority(package, evidence, plan=None, age_days=365):
    """Review priority, NOT a probability of inactivity. Baseline space is excluded."""
    benefit = package.size if plan is None else application_savings(plan)
    score = min(20, math.log2(1 + benefit / 1024**2) * 2)
    reasons = ['Space benefit is ranked without credit for already-unneeded software.']
    if evidence.recorded_age_days is not None and evidence.recorded_age_days >= age_days:
        score += 5
        reasons.append('Older recorded installation: a review hint only.')
    if any(s.get('active') == 'inactive' and s.get('enabled') in ('disabled', 'masked') for s in evidence.services):
        score += 10
        reasons.append('A service is currently stopped and disabled; historical use remains unknown.')
    if evidence.activity:
        score -= 100
        reasons.append('Recent handled request records lower cleanup priority; they may be automated.')
    if evidence.operational:
        score -= 40
        reasons.append('Running software increases disruption risk, independently of whether it is used.')
    if evidence.recent_accesses:
        score -= 10
        reasons.append('Recent file access is weak evidence of possible use.')
    if evidence.modified_configuration or evidence.unreadable_configuration:
        score -= 20
        reasons.append('Changed or unreadable settings increase uncertainty.')
    score -= min(20, len(package.reverse_dependencies) * 2)
    if plan:
        score -= min(20, len(plan.newly_orphaned) / 2)
        score -= 40 * bool(plan.dependent_removals)
        score -= 200 * bool(plan.blocked)
    return score, reasons


@dataclass
class Recommendation:
    package: str
    title: str
    purpose: str
    keep_reason: str
    evidence: Evidence
    score: float
    ranking_reasons: list[str]
    plan: object = None
    error: str | None = None
    assessment: dict = field(default_factory=dict)
    installed_bytes: int = 0
    footprint_components: list[str] = field(default_factory=list)
    footprint_shared: list[str] = field(default_factory=list)
    size_class: str = ""
    provider: str = 'apt'
    identity: str = ''
    installation: str = 'system'
    protected: list[str] = field(default_factory=list)
    component_assessment: ComponentAssessment | None = None


CATEGORIES = {
    'large': 'Large applications',
    'potential': 'Potentially unused applications',
    'servers': 'Servers and development tools',
    'support': 'Unneeded supporting packages',
    'search': 'Search installed applications',
    'advanced': 'System tools and background components',
}


def application_savings(plan):
    # An explicitly selected existing orphan must not be attributed to the application.
    return plan.savings['selected'] + plan.savings['newly_orphaned'] - plan.baseline_selected_bytes


CONFIDENCE_LABELS = {
    'strong_inactivity': 'Strong indications of inactivity',
    'weak_inactivity': 'Weak indications of inactivity',
    'unknown': 'Usage unknown',
    'recent_activity': 'Positive evidence of recent activity',
}


def inactivity_assessment(evidence):
    """Confidence in inactivity is independent of operational/disruption risk.

    Current collectors cannot establish strong historical inactivity. Never promote
    old timestamps, missing logs, or several weak clues to strong evidence.
    """
    reasons, cautions = [], []
    if evidence.operational:
        cautions.append('Software is currently running. Removal would stop it; running alone is not proof of use.')
    if evidence.modified_configuration:
        cautions.append('Settings differ from defaults; the software may have been configured for a purpose.')
    if evidence.unreadable_configuration:
        cautions.append('Some settings could not be inspected.')
    if evidence.activity:
        level = 'recent_activity'
        reasons.append('Access logs record handled requests within 90 days. They may be automated and do not establish human use.')
    elif evidence.recent_accesses:
        level = 'unknown'
        reasons.append('Recent file reads conflict with an inactivity inference, but do not prove execution.')
    elif evidence.executable_count and evidence.old_accesses == evidence.executable_count:
        level = 'weak_inactivity'
        reasons.append("This application's executable files have not recorded recent access. "
                       'That can suggest inactivity, but filesystem timestamps are not a reliable usage history.')
        reasons.append('All inspected executable access timestamps are over a year old; executable coverage may be incomplete.')
    elif any(s.get('active') == 'inactive' and s.get('enabled') in ('disabled', 'masked') for s in evidence.services):
        level = 'weak_inactivity'
        reasons.append('At least one service is stopped and disabled. It may be only a helper; this is not proof that the whole application is unused.')
    else:
        level = 'unknown'
        reasons.append('Available records do not establish activity or inactivity. Missing history is not evidence of non-use.')
    if evidence.recorded_age_days is not None and evidence.recorded_age_days >= 365:
        reasons.append('An older recorded installation adds context, not a last-used date.')
    return {'level': level, 'label': CONFIDENCE_LABELS[level], 'reasons': reasons, 'cautions': cautions}


def potential_clue(evidence):
    assessment = inactivity_assessment(evidence)
    return assessment['reasons'][0] if assessment['level'] in ('strong_inactivity', 'weak_inactivity') else None


def confidence_order(item):
    return {'strong_inactivity': 0, 'weak_inactivity': 1, 'unknown': 2, 'recent_activity': 3}[inactivity_assessment(item.evidence)['level']]


def audit_recommendations(packages, kept=(), limit=5, pool_size=12, progress=None, age_days=365,
              category='large', query=None, diagnostics=None):
    """Separate browsing intent from evidence-based suggestions; never fill unknowns into potential."""
    if category not in ('large', 'potential', 'servers', 'search'):
        raise ValueError('Choose an application category; supporting software uses a separate baseline preview.')
    if category == 'search' and not (query and query.strip()):
        raise ValueError('Enter a name or description to search for.')
    running = running_executables()
    initial = []
    classified = {}
    roots = ([p.name for p in packages.values() if application_root(p) and not p.protected]
             if category in ('servers', 'search') else [names[0] for _, names in review_units(packages)])
    for index, name in enumerate(roots, 1):
        checkpoint("discovery", index, len(roots))
        if progress:
            progress(f'Checking usage records... {index} of {len(roots)}')
        if name in kept:
            continue
        package = packages[name]
        server = package.stack in ('Apache', 'PHP', 'MariaDB / MySQL', 'Development tools')
        if category == 'servers' and not server:
            continue
        title, purpose, keep_reason = describe(package)
        if category == 'search' and query.casefold().strip() not in ' '.join(
                (package.name, package.base, title, purpose, package.summary, package.stack)).casefold():
            continue
        # Service clues need inspection BEFORE shortlisting the potential category.
        evidence = collect_evidence(package, running, inspect_services=category == 'potential')
        classified[name] = inactivity_assessment(evidence)
        if category == 'potential' and not potential_clue(evidence):
            continue
        score, reasons = priority(package, evidence, age_days=age_days)
        initial.append(Recommendation(name, title, purpose, keep_reason, evidence, score, reasons))
    if category in ('servers', 'search'):
        # Family order makes small web/database entry points visible before large compilers.
        order = {'Apache': 0, 'PHP': 1, 'MariaDB / MySQL': 2, 'Development tools': 3}
        initial.sort(key=lambda r: (order.get(packages[r.package].stack, 4) if category == 'servers' else 0, r.package))
    elif category == 'potential':
        initial.sort(key=lambda r: (confidence_order(r), -r.score, r.package))
    else:
        initial.sort(key=lambda r: (-packages[r.package].size, r.package))
    # Only the explicitly size-oriented category uses a preliminary size shortlist.
    # Potential, server and search paths inspect every match, independent of disk size.
    pool = initial[:max(limit, pool_size)] if category == 'large' else initial
    for index, item in enumerate(pool):
        if progress:
            progress(f'Analyzing applications... {index + 1} of {len(pool)}')
            if hasattr(progress, 'diagnostic'):
                progress.diagnostic('Inspecting ' + item.package)
        package = packages[item.package]
        item.evidence = collect_evidence(package, running)
        classified[item.package] = inactivity_assessment(item.evidence)
        item.assessment = classified[item.package]
        try:
            item.plan = simulate([item.package], packages, kept)
        except PackageStateChanged:
            raise
        except (ValueError, OSError) as exc:
            item.error = str(exc)
        item.score, item.ranking_reasons = priority(package, item.evidence, item.plan, age_days=age_days)
    # Direct browsing may expose active/blocked software, with warnings, never bypassing checks.
    eligible = [r for r in pool if r.plan is not None and
                (category in ('servers', 'search') or (not r.plan.blocked and not r.evidence.activity))]
    if category == 'potential':
        eligible = [r for r in eligible if potential_clue(r.evidence)]
        eligible.sort(key=lambda r: (confidence_order(r), -r.score, r.package))
    elif category == 'large':
        eligible.sort(key=lambda r: (-application_savings(r.plan), r.package))
    # Ensure PHP and Apache are discoverable on the first server page even among many compilers.
    if category == 'servers':
        family_order = {'Apache': 0, 'PHP': 1, 'MariaDB / MySQL': 2, 'Development tools': 3}
        eligible.sort(key=lambda r: (family_order.get(packages[r.package].stack, 4), r.package))
    if diagnostics is not None:
        diagnostics.clear()
        diagnostics.update({
            'category': category, 'evaluated': len(classified), 'simulated_candidates': len(pool),
            'status': ('failed' if pool and all(r.plan is None for r in pool) else
                       'partial' if any(r.plan is None for r in pool) else 'complete'),
            'confidence_counts': {level: sum(a['level'] == level for a in classified.values()) for level in CONFIDENCE_LABELS},
            'actionable_count': sum(not r.plan.blocked for r in eligible),
            'blocked': [{'package': r.package, 'title': r.title, 'assessment': classified[r.package],
                         'reasons': r.plan.blocked, 'proposed_removals': r.plan.removed}
                        for r in pool if r.plan is not None and r.plan.blocked],
            'failed': [{'package': r.package, 'error': r.error} for r in pool if r.plan is None],
            'classifications': classified,
        })
    if progress and hasattr(progress, 'finish'):
        progress.finish(len(eligible), failed=sum(r.plan is None for r in pool), attempted=len(pool))
    return eligible


def recommendation_quality(package, paths, desktop_dependencies):
    """Review usefulness, never an assertion that software is obsolete or unused."""
    score, reasons = 0, []
    if package.display_name and package.desktop_application:
        score += 60
        reasons.append('Recognizable desktop application.')
    elif package.base in DESCRIPTIONS:
        score += 30
        reasons.append('Recognizable application or software stack.')
    if set(package.desktop_categories) & {'System', 'Settings', 'Accessibility', 'DesktopSettings'}:
        score -= 100
        reasons.append('Desktop settings, accessibility, or system utility; advanced review preferred.')
    if package.name in desktop_dependencies:
        score -= 100
        reasons.append('Installed as part of the desktop dependency set; advanced review preferred.')
    if any(p.endswith('.service') and '/systemd/' in p for p in paths):
        score -= 45
        reasons.append('Background service; lower beginner-review priority.')
    if not package.desktop_application and package.size < 5 * 1024**2:
        score -= 25
        reasons.append('Small system utility or command-line tool; limited space benefit.')
    if not package.display_name and package.size < 20 * 1024 * 1024:
        score -= 20
        reasons.append('Small component without a visible application name; available in advanced review.')
    if len(package.reverse_dependencies) == 0 and package.application:
        score += 15
        reasons.append('Standalone application with no installed hard reverse dependencies.')
    return score, reasons


def recommend(packages, kept=(), limit=5, pool_size=12, progress=None, age_days=365,
              category='large', query=None, diagnostics=None, component_filter='reviewable'):
    """Fast discovery only. No APT transactions, validation claims, or preview cache."""
    from .providers import subset, effective_kept, eligible_support
    kept = effective_kept(packages, kept)
    if category == 'advanced':
        from .classification import component_recommendations, classify_components, Classification, system_component
        from .state import package_state, PackageStateChanged, state_change_message
        initial = package_state()
        expected = getattr(packages,'state_token',initial)
        if expected != initial:
            raise PackageStateChanged(state_change_message(expected,initial,'since inventory'))
        started = time.perf_counter()
        assessments = classify_components(packages)
        items = component_recommendations(packages,kept,component_filter,assessments=assessments)
        current = package_state()
        if initial != current:
            raise PackageStateChanged(state_change_message(initial,current,'during classification'))
        if diagnostics is not None:
            diagnostics.update(status='complete',blocked=[],failed=[],candidate_count=len(items),
                component_counts={c.value:sum(a.classification == c for a in assessments.values()) for c in Classification},
                system_component_counts={c.value:sum(a.classification == c and system_component(packages[n]) for n,a in assessments.items()) for c in Classification},
                component_classifications={n:asdict(a) for n,a in assessments.items()},
                timings={'discovery_seconds':time.perf_counter()-started,'apt_simulations':0})
        if progress and hasattr(progress,'finish'):
            progress.finish(len(items),discovered=True)
        return items
    if any(p.provider == 'flatpak' for p in packages.values()) or category == 'support':
        apt = subset(packages, 'apt')
        items = (recommend(apt, kept, limit, age_days=age_days, progress=None, category=category,
                           query=query, diagnostics=diagnostics) if category != 'support' else [])
        rows = eligible_support(packages, kept) if category == 'support' else [
            p for p in packages.values() if p.provider == 'flatpak' and p.application and p.name not in kept]
        for p in rows:
            if category == 'large' and p.size < LARGE_MIN:
                continue
            if category == 'search' and (not query or query.casefold().strip() not in
                    ' '.join((p.name,p.base,p.display_name,p.summary)).casefold()):
                continue
            if category not in ('large','search','support'):
                continue  # No historical execution evidence: never infer inactivity.
            e = Evidence(running=p.running)
            items.append(Recommendation(p.name,describe(p)[0],p.summary,'',e,0,[],
                assessment=inactivity_assessment(e),installed_bytes=p.size,provider=p.provider,
                identity=p.identity,installation=p.installation,protected=p.protected))
        items.sort(key=lambda i: (-i.installed_bytes,i.title.casefold(),i.package) if category in ('large','support')
                   else (i.title.casefold(),i.package))
        if diagnostics is not None:
            diagnostics.update(candidate_count=len(items), status='complete', blocked=[],failed=[])
        if progress and hasattr(progress,'finish'):
            progress.finish(len(items),discovered=True)
        return items
    from .inventory import support_packages
    from .state import package_state, state_change_message, PackageStateChanged
    started = time.perf_counter()
    initial_state = package_state()
    expected = getattr(packages, 'state_token', initial_state)
    if expected != initial_state:
        raise PackageStateChanged(state_change_message(expected, initial_state, 'since inventory'))
    if category not in ('large', 'potential', 'servers', 'search', 'advanced'):
        raise ValueError('Choose an application category.')
    if category == 'search' and not (query and query.strip()):
        raise ValueError('Enter an application name or description.')
    desktop_roots = [p.name for p in packages.values() if p.base.startswith('mint-meta-')]
    desktop_dependencies = {x['name'] for x in support_packages(desktop_roots, packages, automatic_only=False)}
    roots = ([p.name for p in packages.values() if application_root(p) and not p.protected]
             if category in ('servers', 'search', 'advanced') else [names[0] for _, names in review_units(packages)])
    running = running_executables()
    items, classified, timings, deferred = [], {}, [], []
    exclusions, observations = {}, {}
    evidence_seconds = 0
    for index, name in enumerate(roots, 1):
        checkpoint("discovery", index, len(roots))
        if progress:
            progress(f'Finding applications... {index} of {len(roots)}')
        if name in kept:
            exclusions[name] = ['Saved Keep preference.']
            continue
        p = packages[name]
        footprint = application_footprint(name, packages)
        if category == 'large' and footprint.installed_bytes < LARGE_MIN:
            exclusions[name] = ['Installed application footprint is below 100 MB.']
            continue
        if category == 'servers' and p.stack not in ('Apache', 'PHP', 'MariaDB / MySQL', 'Development tools'):
            continue
        title, purpose, keep_reason = describe(p)
        if category == 'search' and query.casefold().strip() not in ' '.join((p.name,p.base,title,purpose,p.summary,p.stack if p.stack != 'Other software' else '')).casefold():
            continue
        t = time.perf_counter()
        paths = package_paths(p)
        e_start = time.perf_counter()
        evidence = collect_evidence(p, running, paths=paths)
        elapsed_evidence = time.perf_counter() - e_start
        evidence_seconds += elapsed_evidence
        assessment = inactivity_assessment(evidence)
        classified[name] = assessment
        observations[name] = asdict(evidence)
        score, reasons = priority(p, evidence, age_days=age_days)
        quality, quality_reasons = recommendation_quality(p, paths, desktop_dependencies)
        timings.append({'package': name, 'seconds': time.perf_counter()-t, 'evidence_seconds': elapsed_evidence})
        if category in ('large', 'potential') and quality < 0:
            deferred.append({'package':name, 'reasons':quality_reasons})
            exclusions[name] = quality_reasons
            continue
        if category == 'potential' and not potential_clue(evidence):
            exclusions[name] = assessment['reasons']
            continue
        if category == 'advanced' and quality >= 0:
            continue
        if category in ('large', 'potential') and evidence.activity:
            continue
        item = Recommendation(name,title,purpose,keep_reason,evidence,score+quality,reasons+quality_reasons,
                              assessment=assessment,installed_bytes=footprint.installed_bytes,
                              footprint_components=list(footprint.components), footprint_shared=list(footprint.shared_components),
                              size_class=size_class(footprint.installed_bytes), identity=p.identity, protected=p.protected)
        items.append(item)
        # Stop a common invalid snapshot once, rather than assigning it to every app.
        if index % 20 == 0:
            current = package_state()
            if current != initial_state:
                raise PackageStateChanged(state_change_message(initial_state,current,'during discovery'))
    current = package_state()
    if current != initial_state:
        raise PackageStateChanged(state_change_message(initial_state,current,'during discovery'))
    order={'Apache':0,'PHP':1,'MariaDB / MySQL':2,'Development tools':3}
    items.sort(key=lambda i: ((order.get(packages[i.package].stack,4) if category=='servers' else 0),
                             confidence_order(i) if category=='potential' else 0, -i.score, -i.installed_bytes, i.package))
    if category == 'search' and query:
        for p in packages.values():
            if p.protected and query.strip().casefold() in (p.name.casefold(),p.base.casefold(),p.identity.casefold()):
                items.append(Recommendation(p.name,describe(p)[0],p.summary,'',Evidence(),0,[],
                    installed_bytes=p.size,identity=p.identity,protected=p.protected))
    if category == 'large':
        items.sort(key=lambda i: (-i.installed_bytes, i.title.casefold(), i.package))
    if diagnostics is not None:
        diagnostics.clear()
        diagnostics.update(category=category, status='complete', evaluated=len(classified),
                           candidate_count=len(items), simulated_candidates=0, validated_count=0,
                           blocked=[],failed=[],classifications=classified,deferred_to_advanced=deferred,
                           exclusions=exclusions, observations=observations,
                           confidence_counts={level:sum(a['level']==level for a in classified.values()) for level in CONFIDENCE_LABELS},
                           timings={'discovery_seconds':time.perf_counter()-started,'evidence_seconds':evidence_seconds,
                                    'apt_simulations':0,'apt_seconds':0,'candidates':timings})
    if progress and hasattr(progress,'finish'):
        progress.finish(len(items), discovered=True)
    return items
