# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
import argparse
from dataclasses import asdict
import json
import sys
import time
from .state import PackageStateChanged

from .analysis import review_reason, review_units
from .core.providers import inventory, preview as simulate
from .inventory import resolve, stack_catalog, stack_selection
from .presentation import details, review, show_plan, size, print_recommendation, support_review, choose_category, quit_review, analysis_message, exclusion_summary
from .preferences import Preferences
from .recommendations import recommend, CATEGORIES
from .progress import Progress
from .terminal import ask
from .removal import log_plan


def confirm_clear():
    return ask('Clear all saved Keep choices? This only resets recommendations. [y/N]:') in ('y', 'yes')


def _main():
    parser = argparse.ArgumentParser(description='Mint / Debian APT package review; removal requires explicit opt-in and confirmation.')
    parser.add_argument('--enable-removal', action='store_true', help='Offer explicitly confirmed execution after final validation.')
    parser.add_argument('--log', help='Append review and simulation events to this JSONL file (opt-in local write).')
    parser.add_argument('--preferences', help='Path for saved Keep preferences (default: XDG configuration directory).')
    sub = parser.add_subparsers(dest='action')
    sub.add_parser('gui', help='Open the native GTK interface; removal requires graphical confirmation.')
    scan = sub.add_parser('scan', help='List review candidates, not proven-unused packages.')
    scan.add_argument('--all', action='store_true', help='Include every installed package.')
    scan.add_argument('--json', action='store_true')
    scan.add_argument('--diagnostics', action='store_true', help='Report evidence classifications, discovery timings, and advanced-category exclusions.')
    scan.add_argument('--days', type=int, default=365, help='Recorded install age threshold for the weak age review hint.')
    guided = sub.add_parser('review', help='Answer simple questions and preview cleanup.')
    guided.add_argument('--enable-removal', action='store_true', default=argparse.SUPPRESS, help='Offer explicitly confirmed execution after final validation.')
    guided.add_argument('--limit', type=int, default=5, help='Suggestions per review set (1–20); remaining matches are accessible with Next.')
    scan.add_argument('--limit', type=int, default=5, help='Maximum displayed suggestions (1–20); JSON includes all analyzed matches.')
    for cmd in (scan, guided):
        cmd.add_argument('--verbose', action='store_true', help='Print per-application diagnostics.')
        cmd.add_argument('--category', choices=CATEGORIES, help='Choose a cleanup category.')
        cmd.add_argument('--search', help='Search application names and descriptions.')
        from .core.classification import FILTERS
        cmd.add_argument('--component-filter', choices=FILTERS, default='reviewable', help='System-tools classification or manual support investigation.')
    search = sub.add_parser('search', help='Find an application and review it directly.')
    search.add_argument('query')
    search.add_argument('--verbose', action='store_true')
    search.add_argument('--limit', type=int, default=5)
    prefs = sub.add_parser('preferences', help='List or reset saved Keep choices.')
    prefs.add_argument('operation', choices=['list', 'reset'])
    prefs.add_argument('names', nargs='*', help='Exact kept names (advanced).')
    prefs.add_argument('--all', action='store_true', help='Reset every Keep choice.')
    ignored = sub.add_parser('ignored', help='Manage permanently kept applications.')
    ignored_sub = ignored.add_subparsers(dest='ignored_action', required=True)
    ignored_sub.add_parser('list', help='Show saved package identifiers.')
    remove = ignored_sub.add_parser('remove', help='Allow an application to be suggested again.')
    remove.add_argument('package')
    ignored_sub.add_parser('clear', help='Clear all Keep choices after confirmation.')
    detail = sub.add_parser('details', help='Inspect a package and on-demand usage evidence.')
    detail.add_argument('package')
    sub.add_parser('groups', help='List heuristic software stacks and automatic dependency support.')
    sim = sub.add_parser('simulate', help='Show complete APT removal simulation; never execute it.')
    sim.add_argument('packages', nargs='*')
    sim.add_argument('--json', action='store_true', help='Full plan, retained relationships and raw APT output.')
    sim.add_argument('--stack', action='append', default=[], help='Select a stack only if it has exactly one manual application entry point; otherwise name packages individually.')
    args = parser.parse_args()
    if getattr(args, 'days', 0) < 0:
        parser.error('--days must be nonnegative')
    if not 1 <= getattr(args, 'limit', 5) <= 20:
        parser.error('--limit must be between 1 and 20')
    try:
        if args.action == 'gui':
            from .gui import run
            return run(args.preferences)
        if args.action is None:
            args.category = choose_category()
            if args.category is None:
                return quit_review()
        preferences = Preferences(args.preferences)
        if args.action == 'ignored':
            if args.ignored_action == 'list':
                print('Permanently kept applications (package identifiers):')
                print('\n'.join(sorted(preferences.kept)) or '(none)')
            elif args.ignored_action == 'remove':
                preferences.remove(args.package)
                print('Keep choice removed. The application can be suggested again.')
            elif confirm_clear():
                preferences.reset()
                print('All Keep choices cleared.')
            else:
                print('Cancelled. Keep choices unchanged.')
            return 0
        if args.action == 'preferences':
            if args.operation == 'list':
                print('Saved Keep choices:')
                print('\n'.join(sorted(preferences.kept)) or '(none)')
            elif args.all and not args.names:
                if confirm_clear():
                    preferences.reset()
                    print('All Keep choices reset. Applications can be suggested again.')
                else:
                    print('Cancelled. Keep choices unchanged.')
            elif args.names and not args.all:
                missing = set(args.names) - preferences.kept
                if missing:
                    raise ValueError('Unknown Keep choices; use preferences list for exact names.')
                preferences.reset(args.names)
                print('Selected Keep choices reset.')
            else:
                raise ValueError('Choose exact saved names or --all to reset preferences.')
            return 0
        if args.action in (None, 'review', 'search'):
            return review(None, log=args.log, preferences=preferences, limit=getattr(args, 'limit', 5),
                          category='search' if args.action == 'search' or getattr(args, 'search', None) else getattr(args, 'category', None),
                          query=args.query if args.action == 'search' else getattr(args, 'search', None),
                          verbose=getattr(args, 'verbose', False), enable_removal=args.enable_removal,
                          component_filter=getattr(args,'component_filter','reviewable'))
        inventory_started = time.perf_counter()
        packages, warnings = inventory()
        inventory_seconds = time.perf_counter() - inventory_started
        for warning in warnings:
            print('Warning:', warning, file=sys.stderr)
        candidates = [p for p in packages.values() if review_reason(p, getattr(args, 'days', 365))]
        candidates.sort(key=lambda p: (not p.orphan, p.installed_at or '9999', -p.size, p.name))
        if args.action == 'scan':
            rows = sorted(packages.values(), key=lambda p: p.name) if args.all else candidates
            if not args.all:
                category = 'search' if args.search else (args.category or 'large')
                if category == 'support':
                    return support_review(packages, preferences.kept, as_json=args.json)
                diagnostics = {}
                for attempt in range(2):
                    try:
                        items = recommend(packages, preferences.kept, args.limit, age_days=args.days,
                                          category=category, query=args.search,
                                          progress=Progress(verbose=args.verbose), diagnostics=diagnostics, component_filter=args.component_filter)
                        break
                    except PackageStateChanged:
                        if attempt:
                            raise
                        print("Package information changed; refreshing the scan.", file=sys.stderr)
                        refresh_started = time.perf_counter()
                        packages, warnings = inventory()
                        inventory_seconds += time.perf_counter() - refresh_started
                diagnostics.setdefault('timings', {})['inventory_seconds'] = inventory_seconds
                if args.json:
                    suggestions = [dict(asdict(item), explicit_candidates=[item.package]) for item in items]
                    print(json.dumps({'suggestions': suggestions, 'diagnostics': diagnostics} if args.diagnostics else suggestions, indent=2))
                else:
                    print(CATEGORIES[category])
                    if category == 'large':
                        from .core.footprint import LARGE_DESCRIPTION
                        print(LARGE_DESCRIPTION)
                    if diagnostics.get('status') == 'failed':
                        print(analysis_message(diagnostics))
                    elif diagnostics['blocked'] or diagnostics['failed']:
                        print(exclusion_summary(diagnostics))
                    if args.diagnostics:
                        print(json.dumps(diagnostics, indent=2))
                    for number, item in enumerate(items[:args.limit], 1):
                        print_recommendation(item, number, len(items), category)
                    if len(items) > args.limit:
                        print(f'Showing {args.limit} of {len(items)}. Guided review lets you continue through all matches.')
                    if not items and diagnostics.get('status') != 'failed':
                        print('No applications meet the 100 MB threshold.' if category == 'large' else 'No reviewable applications in this category.')
                    if diagnostics['failed'] and not args.diagnostics:
                        print('Use --diagnostics to inspect the failures.')
                return 1 if diagnostics['failed'] else 0
            if args.json:
                print(json.dumps([dict(asdict(p), review_reason=review_reason(p, args.days),
                                       inactivity_confidence='unknown') for p in rows], indent=2))
            else:
                print(f'{len(packages)} installed; {sum(not p.automatic for p in packages.values())} manual; '
                      f'{sum(bool(p.protected) for p in packages.values())} protected; {len(rows)} shown.')
                print('Review hints only. Unknown dates and old installations do not establish inactivity.')
                for p in rows:
                    print(f'{p.name:50} {size(p.size):>12} {"auto" if p.automatic else "manual":6} '
                          f'{p.installed_at or "date unknown":19} [{p.stack}]'
                          f'{" PROTECTED" if p.protected else ""}')
                    print('  ' + (review_reason(p, args.days) or 'Not selected for review.'))
        elif args.action == 'details':
            details(packages[resolve([args.package], packages)[0]], packages)
        elif args.action == 'groups':
            print(json.dumps(stack_catalog(packages), indent=2))
            print('Membership is context, not removal approval. Each application is reviewed individually. '
                  'Supporting packages, including manual ones, are never selected by grouping.', file=sys.stderr)
        elif args.action == 'simulate':
            names = list(args.packages)
            for stack in args.stack:
                names.extend(stack_selection(stack, packages))
            plan = simulate(names, packages, preferences.kept)
            log_plan(args.log, plan)
            if args.json:
                print(json.dumps(asdict(plan), indent=2))
            else:
                show_plan(plan)
            return 2 if plan.blocked else 0
        else:
            return review(packages, log=args.log, preferences=preferences, limit=getattr(args, 'limit', 5),
                          category='search' if args.action == 'search' or getattr(args, 'search', None) else getattr(args, 'category', None),
                          query=args.query if args.action == 'search' else getattr(args, 'search', None),
                          verbose=getattr(args, 'verbose', False), enable_removal=args.enable_removal,
                          component_filter=getattr(args,'component_filter','reviewable'))
        return 0
    except (RuntimeError, ValueError, OSError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\nCancelled.', file=sys.stderr)
        return 130


def main():
    from .core.operations import operation
    with operation():
        return _main()


if __name__ == '__main__':
    sys.exit(main())
