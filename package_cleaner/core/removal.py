# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""APT simulations only. No privilege elevation or removal implementation."""
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
import json
import re

from .operations import checkpoint
from .analysis import command
from .state import package_state, state_change_message, PackageStateChanged
from .inventory import resolve, support_packages


@dataclass
class Baseline:
    removed: list[str]
    output: str


@dataclass
class Plan:
    requested: list[str]
    removed: list[str]
    newly_orphaned: list[str]
    existing_orphans: list[str]
    dependent_removals: list[str]
    retained: list[dict]
    unselected_manual: list[dict]
    savings: dict[str, int]
    blocked: list[str]
    output: str
    baseline: Baseline
    baseline_selected_bytes: int = 0
    transaction_arguments: list[str] = field(default_factory=list)
    state_token: str | None = None
    retained_ineligible_support: list[str] = field(default_factory=list)
    provider_plans: list = field(default_factory=list)
    execution_disabled: bool = False
    provider: str = 'apt'
    installation: str = 'system'
    data_retained: bool = True
    observations: list[str] = field(default_factory=list)
    newly_unneeded_retained: list[str] = field(default_factory=list)
    operations: list[dict] = field(default_factory=list)


def parse_simulation(output, packages):
    removed = []
    for line in output.splitlines():
        if line.startswith(('Inst ', 'Conf ', 'Purg ')):
            raise ValueError('Unexpected install/configure/purge operation in simulation: ' + line)
        if line.startswith('Remv '):
            removed.extend(resolve([line.split()[1]], packages))
    summary = re.search(r'(?m)^(\d+) upgraded, (\d+) newly installed, (\d+) to remove', output)
    if not summary or int(summary[1]) or int(summary[2]) or int(summary[3]) != len(removed):
        raise ValueError('APT output is incomplete or unsupported; refusing to approve a plan.')
    if len(set(removed)) != len(removed):
        raise ValueError('Duplicate removal records in APT output.')
    return sorted(removed)


def apt_simulation(arguments, packages):
    checkpoint("simulation")
    code, out, err = command(['apt-get', '--simulate', '-o', 'Debug::NoLocking=1',
                              *arguments], timeout=120)
    if code:
        raise ValueError(f'APT simulation failed ({code}):\n{out}\n{err}')
    return parse_simulation(out, packages), out + err


def establish_baseline(packages):
    removed, output = apt_simulation(['autoremove'], packages)
    return Baseline(removed, output)


def classify(requested, direct, removed, baseline, packages, kept, output):
    selected = set(requested)
    all_removed = set(removed)
    preexisting = (all_removed & set(baseline.removed)) - selected
    dependent = set(direct) - selected - preexisting
    newly = all_removed - selected - preexisting - dependent
    if not selected <= all_removed or not set(direct) <= all_removed:
        raise ValueError('Inconsistent APT simulations; refresh inventory and retry.')
    if any(not packages[name].automatic for name in newly):
        raise ValueError('Unexpected manual package in newly orphaned set; refusing classification.')
    blocked = [f'{name}: {", ".join(packages[name].protected)}'
               for name in removed if packages[name].protected]
    blocked += [f'{name}: marked Keep in this session' for name in removed if name in kept]
    unselected_manual = []
    for name in sorted(all_removed - selected):
        if not packages[name].automatic:
            edges = sorted(set(packages[name].dependencies) & all_removed)
            unselected_manual.append({'name': name, 'removed_dependencies': edges,
                                      'explanation': 'Installed Depends/PreDepends relationships to proposed removals; '
                                      'alternatives may overcount. See raw APT output for resolver diagnostics.'
                                      if edges else 'No direct installed dependency edge found; APT proposed this removal. '
                                      'The relationship is unresolved, so explicit selection is still required.'})
            blocked.append(f'{name}: manually installed and not explicitly selected')
    retained = []
    for row in support_packages(requested, packages, automatic_only=False):
        name = row['name']
        if name in all_removed:
            continue
        users = sorted(set(packages[name].reverse_dependencies) - all_removed)
        retained.append({'name': name, 'remaining_reverse_dependencies': users,
                         'reason': 'Remaining installed dependency relationships (alternatives may overcount).'
                         if users else 'Retained by APT; manual flag, recommends, or other APT policy may apply.'})
    categories = {'selected': selected, 'newly_orphaned': newly,
                  'already_orphaned': preexisting, 'dependent_removals': dependent}
    savings = {key: sum(packages[name].size for name in names) for key, names in categories.items()}
    return Plan(sorted(selected), sorted(all_removed), sorted(newly), sorted(preexisting),
                sorted(dependent), retained, unselected_manual, savings, blocked, output, baseline,
                sum(packages[n].size for n in selected & set(baseline.removed)))


def simulate(names, packages, kept=()):
    if any(str(n).startswith('flatpak:') for n in names):
        raise ValueError('Flatpak selections cannot enter the APT transaction path.')
    requested = resolve(names, packages)
    if not requested:
        raise ValueError('Select at least one installed package.')
    initial_state = package_state()
    if getattr(packages, 'state_token', initial_state) != initial_state:
        raise PackageStateChanged(state_change_message(packages.state_token, initial_state, 'since inventory'))

    def checked(arguments):
        current_state = package_state()
        if current_state != initial_state:
            raise PackageStateChanged(state_change_message(initial_state, current_state, 'between simulations'))
        result = apt_simulation(arguments, packages)
        current_state = package_state()
        if current_state != initial_state:
            raise PackageStateChanged(state_change_message(initial_state, current_state, 'during simulation'))
        return result

    # Discovery is not the removal transaction. Its baseline is never silently
    # included in the application-specific target list.
    baseline_names, baseline_output = checked(['autoremove'])
    baseline = Baseline(baseline_names, baseline_output)
    direct, direct_output = checked(['-o', 'APT::Get::AutomaticRemove=false',
                                    'remove', '--', *requested])
    discovered, discovery_output = checked(['--autoremove', 'remove', '--', *requested])
    if not set(requested) <= set(direct) or not set(direct) <= set(discovered):
        raise ValueError('Inconsistent discovery simulations; scan again.')
    new_support = set(discovered) - set(baseline.removed) - set(direct)
    eligible_support = {n for n in new_support if packages[n].automatic
                        and not packages[n].protected and n not in kept}
    targets = sorted(set(requested) | eligible_support)
    arguments = ['-o', 'APT::Get::AutomaticRemove=false', 'remove', '--', *targets]
    removed, final_output = checked(arguments)
    if not set(targets) <= set(removed):
        raise ValueError('Final simulation did not remove the exact targets; scan again.')
    extra = set(removed) - set(targets)
    # Resolver-added packages are dependent removals, never automatically approved
    # applications or inferred orphan support. Keep original user approval intact.
    plan = classify(requested, sorted(set(direct) | extra), removed, baseline,
                    packages, kept,
                    'DISCOVERY WITHOUT AUTOREMOVE:\n' + direct_output +
                    '\nDISCOVERY WITH AUTOREMOVE (NOT THE TRANSACTION):\n' + discovery_output +
                    '\nFINAL EXPLICIT TRANSACTION:\n' + final_output)
    for name in sorted(extra):
        plan.blocked.append(f'{name}: additional resolver removal outside the explicit transaction; separate approval required')
    for name in sorted(set(removed) & set(baseline.removed) - set(requested)):
        plan.blocked.append(f'{name}: existing orphan not separately approved')
    plan.transaction_arguments = arguments
    plan.state_token = initial_state
    plan.retained_ineligible_support = sorted(new_support - eligible_support - set(removed))
    return plan


def log_event(path, action, data):
    with open(path, 'a', encoding='utf-8') as stream:
        stream.write(json.dumps({'time': datetime.now(timezone.utc).isoformat(),
                                 'action': action, 'data': data}) + '\n')


def log_plan(path, plan):
    if path:
        log_event(path, 'simulation', asdict(plan))
