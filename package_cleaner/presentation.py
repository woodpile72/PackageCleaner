# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""Application-focused terminal review and ephemeral decisions."""
import json
import shlex
from .core.providers import inventory, preview as simulate
from .inventory import application_root
from .removal import log_plan, log_event
from .terminal import write, ask, local_date
from .display import installation_label


def size(value):
    return f'{value / 1024**2:.1f} MiB'


def show_plan(plan):
    if plan.provider_plans:
        print('Separate transactions — each needs explicit approval; no atomic rollback.')
        for part in plan.provider_plans:
            print(part.provider.upper() + ': ' + part.installation)
            show_plan(part)
        for reason in plan.blocked + plan.observations:
            print(reason)
        return
    if plan.provider == 'flatpak':
        print('FLATPAK PREVIEW — ' + plan.installation)
        for row in plan.operations:
            print('  Uninstall ' + row['ref'] + ' (' + row['kind'] + ')')
            print('    Installation: ' + row['installation'])
            print('    Authentication required: ' + ('yes' if row['requires_authentication'] else 'not indicated by resolver; system policy may still request it'))
        for note in plan.observations + plan.blocked:
            print(note)
        return
    print('\nAPT SIMULATION — complete proposed removal list by category:')
    categories = [('1. Explicitly selected', plan.requested, 'selected'),
                  ('2. Newly orphaned dependencies', plan.newly_orphaned, 'newly_orphaned'),
                  ('3. Already eligible for autoremove', plan.existing_orphans, 'already_orphaned'),
                  ('Additional dependent removals (NOT orphaned dependencies)', plan.dependent_removals, 'dependent_removals')]
    for title, names, key in categories:
        print(f'\n{title}: {len(names)} packages, {size(plan.savings[key])}')
        for name in names:
            overlap = ' (also in original autoremove baseline)' if name in plan.baseline.removed and key == 'selected' else ''
            print(f'  {name}{overlap}')
    print('\n4. Dependency support retained by APT:')
    for row in plan.retained:
        users = row['remaining_reverse_dependencies']
        summary = ', '.join(users[:3]) + (f' (+{len(users) - 3} more)' if len(users) > 3 else '')
        print('  ' + row['name'] + ': ' + (summary or row['reason']))
    print('Remaining relationships include installed alternatives; they do not prove which alternative APT requires.')
    print(f'\nBaseline: {len(plan.baseline.removed)} packages eligible before the selection.')
    remaining_baseline = sorted(set(plan.baseline.removed) - set(plan.removed))
    if remaining_baseline:
        print('Baseline packages not removed by this plan: ' + ', '.join(remaining_baseline))
    print(f'Total: {len(plan.removed)} packages; installed-size estimate: {size(sum(plan.savings.values()))}')
    print('Categories are disjoint; explicit selection takes precedence. Existing orphan savings are not caused by the selection.')
    print('Actual recovery differs due to retained configuration, shared files and filesystem allocation.')
    if plan.unselected_manual:
        print('\n5. Additional manual packages requiring approval — BLOCKED:')
        for row in plan.unselected_manual:
            print('  ' + row['name'])
            if row['removed_dependencies']:
                print('    Depends/PreDepends on proposed removals: ' + ', '.join(row['removed_dependencies']))
            print('    ' + row['explanation'])
        print('Explicitly add desired packages and rerun the simulation. Protection cannot be overridden.')
    if plan.blocked:
        print('BLOCKED by protection / Keep / explicit-selection policy:')
        for reason in plan.blocked:
            print(f'  {reason}')
    else:
        print('No protection-policy conflict detected. This is not authorization to remove anything.')
    if plan.retained_ineligible_support:
        print('Newly unneeded support retained by policy: ' + ', '.join(plan.retained_ineligible_support))
    if plan.transaction_arguments:
        print('Final transaction simulation command:')
        print(shlex.join(['apt-get', '--simulate', '-o', 'Debug::NoLocking=1', *plan.transaction_arguments]))
    print('Preview only. Plans can become stale as APT state changes.')


def paged_lines(lines, page_size=18):
    """Bound every advanced page, including wrapped long explanations."""
    import textwrap
    import shutil
    width = max(30, min(88, shutil.get_terminal_size((88, 24)).columns))
    wrapped = [part for line in lines for part in (textwrap.wrap(str(line), width) or [''])]
    for offset in range(0, len(wrapped), page_size):
        for line in wrapped[offset:offset + page_size]:
            write(line, 'secondary', width=width)
        if offset + page_size < len(wrapped):
            answer = ask('[N] Next page  [Q] Back:')
            while answer not in ('n', 'q'):
                answer = ask('[N] Next page  [Q] Back:')
            if answer == 'q':
                break


def paged_plan(plan):
    import io
    from contextlib import redirect_stdout
    output = io.StringIO()
    with redirect_stdout(output):
        show_plan(plan)
    paged_lines(output.getvalue().splitlines())


def details(package, packages, evidence=None):
    from .recommendations import describe, collect_evidence, running_executables, inactivity_assessment
    import textwrap
    title, purpose, keep_reason = describe(package)
    evidence = evidence or collect_evidence(package, running_executables())
    short = lambda text: textwrap.shorten(' '.join(str(text).split()), width=180, placeholder='…')
    write('\n' + title, 'title')
    write(short(purpose))
    write('Package: ' + package.base)
    if package.provider == 'apt':
        from .core.classification import classify_components
        assessment = classify_components(packages)[package.name]
        write('Classification: '+assessment.classification.value.title())
        for reason in assessment.reasons:
            write(reason, 'secondary')
    if package.provider == 'flatpak':
        write('Source: Flatpak · ' + installation_label(package.installation))
        write('Ref: ' + package.ref)
        write('Runtime: ' + (package.runtime or 'Not recorded'))
    write('Installed size: ' + friendly_size(package.size) + ' (not a removal estimate).')
    write('Usage observations', 'title')
    write('Confidence: ' + inactivity_assessment(evidence)['label'])
    if evidence.old_accesses and not evidence.recent_accesses:
        write('Inspected program files have no recent access recorded.')
    elif evidence.recent_accesses:
        write('Some program files have recent access recorded.')
    else:
        write('Executable access history is unavailable or incomplete.')
    write('Last observed file access: ' + local_date(evidence.latest_file_access))
    write('File access may reflect backups or reads; it is not a last-used date.', 'secondary')
    if evidence.services:
        running = sum(row.get('active') == 'active' for row in evidence.services)
        enabled = sum(row.get('enabled') == 'enabled' for row in evidence.services)
        write(f'Services: {running} running; {enabled} enabled at boot. This does not establish use.')
    if evidence.server_observations:
        activity = evidence.server_observations.get('application_activity', {})
        write('Last observed application request: ' + local_date(activity.get('last_observed_request')))
        write('Request logs may be incomplete; requests may be automated.', 'secondary')
    if evidence.operational or package.protected or evidence.modified_configuration:
        write('Removal considerations', 'title')
    if evidence.modified_configuration:
        write('Its settings differ from the installed defaults.', 'warning')
    if evidence.operational:
        write('Warning: removing this application would stop its running software.', 'warning')
    if package.protected:
        write('BLOCKED: this is protected system software.', 'blocked')
    write('[T] Advanced package information', 'actions')


def technical_details(package, packages):
    paged_lines([f'Package: {package.name}', f'Version: {package.version}',
                 'Installed manually: ' + ('no' if package.automatic else 'yes'),
                 'Installed on: ' + local_date(package.installed_at),
                 'Direct requirements (not a removal list):', *package.dependencies,
                 'Installed software with a direct dependency:', *package.reverse_dependencies])


def staged_preview(item, plan, packages):
    from .recommendations import describe, application_savings
    write('Queued for removal: ' + item.title, 'title')
    if plan.blocked:
        write('BLOCKED: the combined selection cannot proceed. See Finish for review.', 'blocked')
        write('Recoverable space is not validated for this blocked proposal.', 'warning')
    else:
        write(('Potential recovery upper bound: ' if plan.provider_plans or plan.provider == 'flatpak' else 'Estimated recoverable space for the removal queue: ') + friendly_size(application_savings(plan)), 'success')
    affected = [describe(packages[r['name']])[0] for r in plan.unselected_manual]
    if affected:
        write('Other applications requiring approval: ' + ', '.join(affected[:3]) +
              (f' (+{len(affected)-3} more; see Finish)' if len(affected)>3 else ''), 'warning')
    elif plan.dependent_removals:
        write(f'Warning: {len(plan.dependent_removals)} other software items are affected; see Finish.', 'warning')
    if item.evidence.operational:
        write('Warning: removal would stop running software.', 'warning')



def friendly_size(value):
    if value >= 1024**3:
        return f'{value / 1024**3:.1f} GB'
    return f'{value / 1024**2:.1f} MB'


def plain_plan(plan, packages, final=False):
    from .recommendations import describe
    def say(text):
        role = ('blocked' if text.startswith('BLOCKED') else
                'warning' if text.startswith(('Warning:', '  Separate approval', 'Space figures')) else
                'success' if ('space' in text.lower() or text.startswith('No removal-policy')) and not plan.blocked else
                'title' if text.startswith('\n') else 'normal')
        if text.startswith(('Actual space', 'Preview only.', 'Already-unneeded')):
            role = 'secondary'
        write(text, role)
    say('\n' + ('Your cleanup summary' if final else 'Cleanup preview'))
    applications = [n for n in plan.requested if application_root(packages[n])]
    say('Applications selected for removal: ' + str(len(applications)))
    if len(applications) != len(plan.requested):
        say(f'Other software items separately approved: {len(plan.requested) - len(applications)}')
    for name in plan.requested:
        say('  ' + describe(packages[name])[0])
    say(f'Additional unused supporting items: {len(plan.newly_orphaned)}')
    shared = sum(bool(row['remaining_reverse_dependencies']) for row in plan.retained)
    say(f'Shared supporting items will remain installed: {shared}')
    other_retained = len(plan.retained) - shared
    if other_retained:
        say(f'Other supporting items left installed: {other_retained}')
    from .recommendations import application_savings
    caused = application_savings(plan)
    say(f'Approximate space from your selection and its unused support: {friendly_size(caused)}')
    say(f'Already-unneeded items: {len(set(plan.baseline.removed) & set(plan.removed))} '
          f'({friendly_size(plan.savings["already_orphaned"] + plan.baseline_selected_bytes)} separately; not caused by your selection).')
    remaining_baseline = set(plan.baseline.removed) - set(plan.removed)
    if remaining_baseline:
        say(f'Already-unneeded items left installed for separate review: {len(remaining_baseline)}.')
    if plan.retained_ineligible_support:
        say(f'Supporting items retained by protection or approval rules: {len(plan.retained_ineligible_support)}.')
    if plan.dependent_removals:
        say(f'Warning: {len(plan.dependent_removals)} other installed items would also be removed. '
              'These are affected software, not unused support. Check More information.')
    say(f'Total proposed space recovery: {friendly_size(sum(plan.savings.values()))}')
    if plan.blocked:
        say('BLOCKED: this proposal cannot proceed. It affects protected software, something you kept, '
              'or additional removals you have not approved.')
        for row in plan.unselected_manual:
            say('  Separate approval needed: ' + describe(packages[row['name']])[0])
        say('Space figures above are a blocked proposal, not promised savings.')
    else:
        say('Ready for review.')
    if final:
        say('Actual space may differ. Your documents and application data have not been assessed.')


def choose_category():
    write('\nWhat would you like to review?', 'question')
    write('[1] Large applications\n[2] Potentially unused applications\n[3] Servers and development tools'
          '\n[4] Unneeded supporting packages\n[5] Search installed applications\n[6] System tools and background components\n[H] Help  [Q] Quit', 'actions')
    options = {'1': 'large', '2': 'potential', '3': 'servers', '4': 'support', '5': 'search', '6': 'advanced'}
    while True:
        answer = ask('Choose a category: ')
        if answer == 'h':
            review_help()
        if answer == 'q':
            return None
        if answer in options:
            return options[answer]


def support_review(packages, kept=(), as_json=False):
    from .core.reports import supporting_report
    report = supporting_report(packages, kept)
    blockers = report['blocked']
    if as_json:
        print(json.dumps(report, indent=2))
        return 2 if blockers else 0
    if not report['already_orphaned']:
        print('No supporting packages can be safely removed.')
        return 0
    print(f'\nUnneeded supporting software: {len(report["already_orphaned"])} items, approximately {friendly_size(report["installed_bytes"])}.')
    print('These are already eligible for cleanup, independently of removing any application.')
    if blockers:
        print('BLOCKED: the preview includes protected, kept, or separately installed software.')
    while ask('[D] Details  [Q] Quit: ') == 'd':
        paged_lines([name + (' — BLOCKED' if name in blockers else '') for name in report["already_orphaned"]])
    quit_review()
    return 2 if blockers else 0


def server_findings(item):
    evidence = item.evidence
    name = item.package.split(':')[0]
    rows = evidence.services
    primary = next((r for r in rows if r['unit'] == name + '.service'), None)
    if primary is None and len(rows) == 1:
        primary = rows[0]
    boot = primary.get('enabled') if primary else None
    active = primary.get('active') if primary else None
    write('Start at boot: ' + (boot or 'unknown') + '.', 'secondary')
    write('Currently running: ' + ('yes' if active == 'active' else 'no (inactive)' if active == 'inactive' else active or 'unknown') + '.')
    observations = evidence.server_observations
    listener = observations['listening']
    write('Listening for connections: ' + ('observed with matching process ownership.' if listener['state'] == 'listening'
                                         else 'unknown — socket ownership could not be verified.'), 'secondary')
    activity = observations['application_activity']
    count = activity.get('recent_request_records', 0)
    if count:
        write(f'Application activity: {count} handled HTTP requests recorded in the last 90 days.', 'warning')
        counts = activity.get('recent_status_counts', {})
        write('Recorded response codes: ' + ', '.join(f'{code}: {number}' for code, number in sorted(counts.items())) + '.', 'secondary')
        if not activity.get('recent_successful_responses'):
            write('No successful (2xx) responses in those recent records. Requests may be probes or failed visits.', 'warning')
    else:
        write('Application activity: no recent handled requests established from the available records; usage remains unknown.', 'warning')
    last = activity.get('last_observed_request')
    write('Last observed handled request: ' + (local_date(last) + ' (not a last-human-use date).' if last else 'unknown.'), 'secondary')
    if activity.get('unreadable_files') or activity.get('truncated') or activity.get('unparsed_records'):
        write('Some history was inaccessible, limited, or unrecognized. See Details.', 'warning')
    write('Log retention is incomplete; startup, running and listening are separate from application use.', 'secondary')


def print_recommendation(item, number, count, category='large'):
    from .recommendations import evidence_summary, potential_clue, application_savings, inactivity_assessment
    write(f'\nReviewing suggestion {number} of {count}', 'secondary')
    write(item.title, 'title')
    write('Flatpak' if item.provider == 'flatpak' else 'APT', 'secondary')
    write(item.purpose)
    if item.component_assessment:
        write('Classification: '+item.component_assessment.classification.value.title(), 'secondary')
    write()
    assessment = inactivity_assessment(item.evidence)
    write('Confidence: ' + assessment['label'], 'warning')
    for caution in assessment['cautions']:
        write(caution, 'warning')
    clue = potential_clue(item.evidence)
    if item.evidence.server_observations:
        server_findings(item)
    if clue:
        write('No recent executable-file access recorded.' if item.evidence.old_accesses else
              'A service is stopped and disabled.', 'warning')
    elif not item.evidence.server_observations and (item.evidence.activity or item.evidence.operational or item.evidence.modified_configuration or item.evidence.recent_accesses):
        write(evidence_summary(item.evidence), 'warning')
    elif assessment['level'] == 'unknown' and not item.evidence.server_observations:
        write('Usage history unavailable.', 'secondary')
    if item.plan is None:
        write(f'Installed application size: {friendly_size(item.installed_bytes)} (not a removal estimate).', 'secondary')
    else:
        benefit = application_savings(item.plan)
        write(f'Potential space from this application and newly unneeded support: {friendly_size(benefit)}.',
              'warning' if item.plan.blocked else 'success')
        if item.plan.blocked:
            write('BLOCKED: this preview affects protected, kept, or other unapproved software. Details explains why.', 'blocked')
    if item.evidence.activity:
        write('Handled requests were observed; they may be automated. Check the service’s purpose before removing it.', 'warning')
    if item.plan and item.plan.dependent_removals:
        write('Warning: other installed software would also be affected. Review Details carefully.', 'warning')


def quit_review(selected=()):
    if selected:
        print('Queued removal choices discarded.')
    print('Goodbye.')
    return 0


def review(packages, candidates=None, log=None, preferences=None, limit=5, category=None, query=None, verbose=False, enable_removal=False, initial_selected=(), component_filter='reviewable'):
    from .preferences import Preferences
    from .recommendations import recommend, describe
    from .progress import Progress
    from .state import PackageStateChanged
    preferences = preferences if preferences is not None else Preferences()
    from .core.review_service import ReviewService
    session = ReviewService(preferences, selection_path=False,
                            inventory_provider=lambda: inventory(),
                            simulation_provider=lambda *args: simulate(*args))
    for name in initial_selected:
        session.select(name)
    selected = session.selected
    category = category or ('large' if candidates is not None else choose_category())
    if category is None:
        return quit_review(selected)
    if category == 'support':
        while True:
            try:
                packages, warnings = inventory()
                return support_review(packages, preferences.kept)
            except (ValueError, OSError, RuntimeError) as exc:
                diagnostic = {'failed': [{'package': 'Supporting software', 'error': str(exc)}], 'blocked': []}
                if not retry_analysis(diagnostic):
                    quit_review()
                    return 1
    if category == 'search' and query is None:
        query = ask('Application name or description (Q to quit): ')
        if query == 'q':
            return quit_review(selected)
    diagnostics = {}
    if candidates is None:
        refreshes = 0
        while True:
            # Menu/search input may take minutes. Never reuse pre-menu inventory.
            progress = Progress(verbose=verbose)
            try:
                packages, warnings = inventory()
                preferences.reload()
                items = recommend(packages, preferences.kept, limit,
                                  progress=progress, category=category,
                                  query=query, diagnostics=diagnostics, component_filter=component_filter)
            except PackageStateChanged as exc:
                if refreshes == 0:
                    refreshes += 1
                    write('Package information changed; refreshing the scan.', 'warning')
                    continue
                items = []
                diagnostics = {'status':'failed','blocked':[], 'failed':[{'package':'Discovery','error':str(exc)}]}
                progress.finish(0, failed=1, attempted=1, discovered=True)
            except (ValueError, OSError, RuntimeError) as exc:
                items = []
                diagnostics = {'status': 'failed', 'blocked': [],
                               'failed': [{'package': 'Analysis', 'error': str(exc)}]}
                progress.finish(0, failed=1, attempted=1, discovered=True)
            if diagnostics.get('status') != 'failed':
                break
            if not retry_analysis(diagnostics):
                quit_review()
                return 1
    else:
        items = candidates
    if diagnostics.get('failed') or diagnostics.get('blocked'):
        write(exclusion_summary(diagnostics), 'secondary')
    if category == 'large':
        from .core.footprint import LARGE_DESCRIPTION
        write(LARGE_DESCRIPTION, 'secondary')
    if not items:
        print('No applications meet the 100 MB threshold.' if category == 'large' else 'No reviewable applications in this category.')
        if diagnostics.get('failed') or diagnostics.get('blocked'):
            while ask('[D] Details  [Q] Quit:') == 'd':
                analysis_details(diagnostics)
        quit_review()
        return 1 if diagnostics.get('failed') else 0
    position = 0
    show_card = True
    preview_errors = {}
    while position < len(items):
        preferences.reload()
        item = items[position]
        if show_card:
            print_recommendation(item, position + 1, len(items), category)
        show_card = True
        if item.package in selected:
            print('Currently included in your cleanup preview.')
        if item.package in preferences.kept or item.identity in preferences.kept:
            print('Currently marked Keep; future scans will leave it out.')
        protected = bool(packages[item.package].protected)
        write('[K] Keep permanently  ' + ('' if protected else '[R] Queue Removal  ') + '[S] Skip  [P] Previous  [D] Details  [T] Advanced package information  [F] Finish  [Q] Quit', 'actions')
        answer = ask('What would you like to do? ' + ('[K/S/P/D/T/F/Q]:' if protected else '[K/R/S/P/D/T/F/Q]:'), 'question')
        if answer == 'q':
            return quit_review(selected)
        if answer == 'f':
            break
        if answer == 'p':
            position = max(0, position - 1)
            continue
        if answer == 'd':
            details(packages[item.package], packages, item.evidence)
            if item.package in preview_errors:
                write("Preview could not be verified. Use [T] for the failure reason.", "warning")
            show_card = False
            continue
        if answer == 't':
            technical_details(packages[item.package], packages)
            if item.package in preview_errors:
                paged_lines(preview_errors[item.package].splitlines())
            show_card = False
            continue
        if answer == 'k':
            session.keep(item.package)
            print('Kept. This application will not be suggested again unless you reset its preference.')
        elif answer == 'r':
            if packages[item.package].protected:
                print('This package is protected and cannot be queued for removal.')
                continue
            session.select(item.package, replace_keep=True)
            # Re-read metadata: earlier recommendation previews are not current approval.
            try:
                preview = session.validate()
                fresh, warnings = session.packages, preview.warnings
                if verbose:
                    for warning in warnings:
                        write(warning, 'secondary')
                print('Checking your removal selection…')
                plan = preview.plan
                log_plan(log, plan)
                for name in selected:
                    preview_errors.pop(name, None)
                staged_preview(item, plan, fresh)
            except (ValueError, OSError) as exc:
                for name in selected:
                    preview_errors[name] = str(exc)
                print('Could not check this selection. Return to Details for more information.')
                diagnostics.setdefault('failed', []).append({'package': ', '.join(sorted(selected)), 'error': str(exc)})
                if log:
                    log_event(log, 'preview_error', str(exc))
        elif answer != 's':
            continue
        if log:
            log_event(log, 'review', {'explicit_candidates': [item.package], 'decision': answer})
        position += 1
        if position < len(items) and position % limit == 0:
            answer = ask(f'{len(items) - position} more in this category. [N] Next set  [F] Finish  [Q] Quit: ')
            if answer == 'q':
                return quit_review(selected)
            while answer not in ('n', 'f', 'q'):
                answer = ask('[N] Next set  [F] Finish  [Q] Quit:')
            if answer == 'q':
                return quit_review(selected)
            if answer != 'n':
                break
    if not selected:
        print('\nYour cleanup summary: no applications selected.')
        return 0
    print('\nRechecking your complete selection against the current installed software…')
    while True:
        try:
            preview = session.validate()
            fresh, warnings = session.packages, preview.warnings
            plan = preview.plan
        except (ValueError, OSError) as exc:
            if log:
                log_event(log, 'final_preview_error', str(exc))
            if retry_analysis({'failed': [{'package': 'Complete selection', 'error': str(exc)}], 'blocked': []}):
                fresh, warnings = inventory()
                continue
            quit_review(selected)
            return 2
        log_plan(log, plan)
        cleanup_summary(plan, fresh)
        while True:
            execute_option = '[E] Execute removal  ' if enable_removal and not plan.blocked and not plan.execution_disabled else ''
            answer = ask(execute_option + '[B] Back to review  [D] Details  [A] Review affected software  [Q] Quit without removing:')
            if answer == 'b':
                return review(fresh, candidates=items, log=log, preferences=preferences, limit=limit,
                              category=category, query=query, verbose=verbose, enable_removal=enable_removal,
                              initial_selected=selected, component_filter=component_filter)
            if answer == 'e' and enable_removal and not plan.blocked and not plan.execution_disabled:
                outcome = execute_selection(plan, fresh, preferences, summary_shown=True)
                if outcome is not None:
                    return outcome
                continue
            if answer == 'd':
                paged_plan(plan)
                continue
            if answer in ('q', 'f'):
                quit_review(selected)
                return 2 if plan.blocked else 0
            if answer != 'a':
                continue
            additions = set()
            for name in session.affected_applications(plan):
                title, purpose, keep_reason = describe(fresh[name])
                print(f'\nAlso affected: {title}\n{purpose}\n{keep_reason}')
                print('Your current selection would also remove this software. It needs a separate decision.')
                choice = ask('[R] Queue Removal — add to preview  [K] Keep permanently  [S] Skip  [D] Details  [Q] Quit: ')
                while choice == 'd':
                    details(fresh[name], fresh)
                    choice = ask('[R] Queue Removal — add to preview  [K] Keep permanently  [S] Skip  [Q] Quit: ')
                if choice == 'q':
                    return quit_review(selected | additions)
                if choice == 'r':
                    additions.add(name)
                elif choice == 'k':
                    session.keep(name)
            if not additions:
                print('No additional software approved. Any blocking conditions remain.')
                continue
            for name in additions:
                session.select(name)
            if log:
                log_event(log, 'explicitly_add', sorted(additions))
            fresh, warnings = inventory()
            break


def review_help():
    write('Choose a category, then review one application at a time. [K] Keep permanently saves your choice; [R] Queue Removal checks the proposed changes; [S] Skip asks again next time. At a set boundary, [N] Next set continues the review. [P] Previous revisits a suggestion; [D] Details shows more information. [F] Finish shows your selected cleanup; [Q] Quit discards it.')
    write('Removal is available only with --enable-removal, followed by Execute and exact REMOVE confirmation. Usage records may be incomplete, and old file timestamps do not prove non-use. Details explains the current application. Advanced package information and the final detailed proposal are paginated.')


def exclusion_summary(diagnostics):
    parts = []
    if diagnostics.get('blocked'):
        parts.append(f"{len(diagnostics['blocked'])} blocked")
    if diagnostics.get('failed'):
        parts.append(f"{len(diagnostics['failed'])} could not be checked")
    return '; '.join(parts) + '. See Details.'


def analysis_message(diagnostics):
    errors = [row['error'] for row in diagnostics.get('failed', [])]
    if errors and all('Package state changed' in error for error in errors):
        return 'Analysis failed: package information changed. Retry with a fresh scan.'
    return 'Analysis failed: the proposed removals could not be checked. Details explains why.'


def analysis_details(diagnostics):
    from collections import defaultdict
    errors = defaultdict(list)
    for row in diagnostics.get('failed', []):
        errors[row['error']].append(row['package'])
    lines = []
    for error, names in errors.items():
        lines.append(f'{len(names)} failed previews: ' + ', '.join(names))
        lines.append(error)
    for row in diagnostics.get('blocked', []):
        lines.append(row['package'] + ': ' + '; '.join(row['reasons']))
    paged_lines(lines)


def retry_analysis(diagnostics):
    write(analysis_message(diagnostics), 'warning')
    while True:
        answer = ask('[R] Retry  [D] Details  [Q] Quit:')
        if answer == 'd':
            analysis_details(diagnostics)
        elif answer == 'r':
            return True
        elif answer == 'q':
            return False


def cleanup_summary(plan, packages):
    if len(plan.provider_plans) == 1:
        return cleanup_summary(plan.provider_plans[0], packages)
    from .recommendations import describe, application_savings
    write('\nYour cleanup summary', 'title')
    write(('Validated preview — execution disabled:' if plan.execution_disabled else 'Ready to remove:') if not plan.blocked else 'BLOCKED — this selection cannot be removed:',
          'title' if not plan.blocked else 'blocked')
    # Keep long selections readable too.
    paged_lines(['  ' + describe(packages[n])[0] for n in plan.requested])
    if plan.provider == 'flatpak' and not plan.provider_plans:
        write(installation_label(plan.installation))
    write(f'Additional unused supporting packages: {len(plan.newly_orphaned)}')
    write('No validated removal transaction.' if plan.blocked else f'Total packages to remove: {len(plan.removed)}')
    if not plan.blocked:
        write(('Potential recovery upper bound: ' if plan.provider_plans or plan.provider == 'flatpak' else 'Estimated space recovered: ') + friendly_size(application_savings(plan)), 'success')
    if plan.provider == 'flatpak' and not plan.provider_plans:
        write('Application data will be kept. Shared storage means actual recovery may be smaller.', 'secondary')
    else:
        for note in plan.observations:
            write(note, 'secondary')
        write('Existing unrelated orphaned packages: left alone.', 'secondary')
    if plan.blocked:
        write('Protected, kept or unapproved software is affected. Details explains the blocking conditions.', 'warning')


def execute_selection(plan, packages, preferences, *, summary_shown=False):
    if plan.provider_plans:
        from .core.authorization import execution_parts, refresh_execution_part
        parts = execution_parts(plan)
        if len(parts) > 1:
            write('These are independent transactions. Each requires its own REMOVE confirmation; completed removals cannot be rolled back.', 'warning')
        for part in parts:
            previous = part
            try:
                part, packages = refresh_execution_part(part, preferences)
            except (ValueError, OSError) as exc:
                write('Stopped before the next transaction: ' + str(exc), 'warning')
                return 2
            unchanged = all(getattr(previous, key) == getattr(part, key) for key in
                            ('provider', 'installation', 'requested', 'removed', 'newly_orphaned',
                             'dependent_removals', 'savings', 'operations', 'blocked'))
            already_shown = len(parts) == 1 and summary_shown and unchanged
            if summary_shown and len(parts) == 1 and not unchanged:
                write('The proposal changed during revalidation. Review the updated summary:', 'warning')
            outcome = execute_selection(part, packages, preferences, summary_shown=already_shown)
            if outcome != 0:
                if len(parts) > 1:
                    write('Stopped. No further provider transaction was started.', 'warning')
                return outcome
        return 0
    from .core.authorization import prepare_authorization, execute_confirmed, is_confirmation
    from .recommendations import describe
    try:
        record = prepare_authorization(plan, packages, preferences)
    except (ValueError, OSError, RuntimeError) as exc:
        write('Cannot execute: ' + str(exc), 'warning')
        return 2
    if not summary_shown:
        cleanup_summary(plan, packages)
    write('Type REMOVE to continue:', 'question', end=' ')
    try:
        confirmation = input()  # Exact case-sensitive confirmation, no normalization.
    except (EOFError, KeyboardInterrupt):
        confirmation = ''
    if not is_confirmation(confirmation):
        write('Execution cancelled.')
        return None
    for name in plan.requested:
        write('Removing ' + describe(packages[name])[0] + '…')
    if plan.newly_orphaned:
        write('Removing unused supporting packages…')
    try:
        result = execute_confirmed(record, confirmation, enabled=True)
    except (OSError, ValueError) as exc:
        write('Execution status could not be verified: ' + str(exc), 'warning')
        write('Reconciliation required. Check the saved diagnostics before retrying.', 'warning')
        return 2
    removed = set(result.get('removed', []))
    completed = result.completed
    if completed:
        write('Cleanup complete.', 'success')
    elif result['status'] == 'unknown' or not result.get('helper_verified'):
        write('Execution status unknown; reconciliation required. Check the saved diagnostics before retrying.', 'warning')
    elif result['status'] == 'partial':
        write('Cleanup partially completed. Completed removals were not rolled back.', 'warning')
    elif result['status'] == 'cancelled':
        write('Execution cancelled before changes.')
    elif result['status'] == 'failed':
        write('Execution failed without changes.', 'warning')
    elif result['status'] == 'rejected':
        write('Removal stopped before execution. Review the selection again.', 'warning')
    else:
        write('Cleanup did not complete. Do not retry until the package state has been checked.', 'warning')
    write(f'Applications removed: {len(removed & set(plan.requested))}')
    write(f'Supporting packages removed: {len(removed & set(plan.newly_orphaned))}')
    if removed:
        estimate = sum(packages[n].size for n in removed if n in packages)
        write('Removed software occupied approximately ' + friendly_size(estimate) +
              '. Actual disk space recovery was not measured.', 'secondary')
    if result.get('unknown'):
        write(f'Outcome unknown or incomplete for {len(result["unknown"])} packages.', 'warning')
    if result.get('remaining'):
        write(f'{len(result["remaining"])} selected packages remain installed.')
    if result.get('error'):
        write(result['error'].splitlines()[0][:180], 'warning')
    if not completed and plan.provider == 'apt':
        write('Inspect the saved diagnostics. An administrator can check package health with dpkg --audit; '
              'do not run repair or rollback commands without reviewing their effects.', 'secondary')
    write('Execution log: ' + result['log_directory'], 'secondary')
    return 0 if completed else 2
