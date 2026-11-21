#!/usr/bin/env python3
"""Validate that the replay ledger files agree with each other.

The tracker is the source of truth for the whole replay: tasks.py picks
work from it, the README tables are generated from it, and reviewers credit
modules from it.  Nothing else checks that its files stay consistent, so
this script cross-checks them:

  tracker/summary.json        headline counts and pins
  tracker/modules.jsonl.gz    one row per module (the snapshot)
  tracker/completed.tsv       post-snapshot evidence overlay (see load()
                              in tasks.py: a completed id's snapshot state
                              is replaced by its evidence value, so the two
                              state models are checked separately here)
  tracker/packets.tsv         per-packet aggregates of the snapshot
  evidence/**/*.json          build receipts and other evidence records

Every check prints one PASS/FAIL line; the exit status is nonzero if any
check fails.  Run with no arguments to validate the repository this file
lives in, or pass a repository root to validate a copy elsewhere.
"""
from collections import Counter, defaultdict
from pathlib import Path
import csv, gzip, json, re, sys

# Evidence categories documented in completed/README.md ("Completed parts").
# A value outside this set means a new category was introduced without
# updating the documentation, so it is a hard failure on purpose.
KNOWN_EVIDENCE = {
    'build-observed',
    'pilot-reused',
    'pilot-witness-checked',
    'volunteer-build-automated',
    'volunteer-build-reviewed',
    'wave-reused',
    'worker-kernel-checked',
}
failures = []
ran = 0


def check(name, ok, detail=''):
    global ran
    ran += 1
    print(('PASS' if ok else 'FAIL'), name, ('-- ' + detail if detail else ''))
    if not ok:
        failures.append(name)
    return ok


def examples(items, limit=3):
    items = list(items)
    shown = ', '.join(str(i) for i in items[:limit])
    return shown + (f' (+{len(items) - limit} more)' if len(items) > limit else '')


def main():
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
    tracker = root / 'tracker'

    # ---- Load the ledger -------------------------------------------------
    try:
        summary = json.loads((tracker / 'summary.json').read_text())
    except Exception as e:
        check('summary.json loads', False, str(e))
        print(f'FAILED: {len(failures)} of {ran} checks failed')
        return 1
    try:
        with gzip.open(tracker / 'modules.jsonl.gz', 'rt') as f:
            rows = [json.loads(line) for line in f if line.strip()]
    except Exception as e:
        check('modules.jsonl.gz loads', False, str(e))
        return 1
    check('modules.jsonl.gz loads', True, f'{len(rows)} rows')

    # ---- modules.jsonl.gz against summary.json (snapshot state model) ----
    check('modules.count', len(rows) == summary['module_count'],
          f"{len(rows)} rows, summary.module_count={summary['module_count']}")
    packets = {r.get('packet') for r in rows}
    check('modules.packets', len(packets) == summary['packet_count'],
          f"{len(packets)} distinct packets, summary.packet_count={summary['packet_count']}")
    state_counts = Counter(r.get('state') for r in rows)
    check('modules.state_counts', dict(state_counts) == summary['state_counts'],
          f'snapshot states {dict(state_counts)} vs summary {summary["state_counts"]}')
    def field_ok(r):
        return (isinstance(r.get('id'), str) and isinstance(r.get('module'), str)
                and isinstance(r.get('packet'), str) and isinstance(r.get('state'), str)
                and isinstance(r.get('depth'), int) and isinstance(r.get('imports'), list))
    missing = [r.get('id', '?') for r in rows if not field_ok(r)]
    check('modules.fields', not missing,
          'every row has id/module/packet/state strings, an integer depth and an imports list'
          if not missing else 'rows with missing or mistyped fields: ' + examples(missing))
    ids = [r['id'] for r in rows]
    check('modules.unique_ids', len(set(ids)) == len(ids),
          f'{len(ids) - len(set(ids))} duplicate module ids')
    by_id = {r['id']: r for r in rows}
    bad_imports = [(r['id'], i) for r in rows for i in r['imports'] if i not in by_id]
    check('modules.imports_resolve', not bad_imports,
          f'{sum(len(r["imports"]) for r in rows)} import references checked'
          if not bad_imports else 'unresolved imports: ' + examples(bad_imports))

    # ---- completed.tsv (the post-snapshot overlay) ------------------------
    completed_path = tracker / 'completed.tsv'
    if not check('completed.exists', completed_path.exists(), str(completed_path)):
        completed = []
    else:
        with completed_path.open() as f:
            completed = list(csv.DictReader(f, delimiter='\t'))
    comp_ids = [r['id'] for r in completed]
    check('completed.unique_ids', len(set(comp_ids)) == len(comp_ids),
          f'{len(comp_ids) - len(set(comp_ids))} duplicate ids in {len(completed)} rows')
    check('completed.sorted', comp_ids == sorted(comp_ids),
          'rows are sorted by id' if comp_ids == sorted(comp_ids)
          else 'rows are not sorted by id')
    unknown = [r['id'] for r in completed if r['id'] not in by_id]
    check('completed.ids_exist', not unknown,
          'all ids exist in modules.jsonl.gz' if not unknown
          else 'unknown ids: ' + examples(unknown))
    mismatched = [r['id'] for r in completed if r['id'] in by_id and
                  (by_id[r['id']]['packet'] != r['packet'] or by_id[r['id']]['module'] != r['module'])]
    check('completed.packet_module_match', not mismatched,
          'packet and module columns match the module records' if not mismatched
          else 'mismatched rows: ' + examples(mismatched))
    bad_evidence = sorted({r['evidence'] for r in completed} - KNOWN_EVIDENCE)
    check('completed.evidence_known', not bad_evidence,
          f"evidence values in use: {sorted({r['evidence'] for r in completed})}"
          if not bad_evidence else 'undocumented evidence values: ' + examples(bad_evidence))

    # ---- packets.tsv against the snapshot --------------------------------
    packets_path = tracker / 'packets.tsv'
    if not check('packets.exists', packets_path.exists(), str(packets_path)):
        packet_list = []
    else:
        with packets_path.open() as f:
            packet_list = list(csv.DictReader(f, delimiter='\t'))
    packet_ids = [r['packet'] for r in packet_list]
    dup_packets = sorted({p for p in packet_ids if packet_ids.count(p) > 1})
    check('packets.unique_ids', not dup_packets,
          f'packet ids are unique in {len(packet_list)} rows' if not dup_packets
          else 'duplicate packet rows: ' + examples(dup_packets))
    packet_rows = {r['packet']: r for r in packet_list}
    count = Counter(r['packet'] for r in rows)
    open_count = Counter(r['packet'] for r in rows if r['state'] == 'open')
    depths = defaultdict(set)
    members = defaultdict(set)
    for r in rows:
        depths[r['packet']].add(r['depth'])
        members[r['packet']].add(r['module'])
    check('packets.set_matches', set(packet_rows) == set(count),
          f'{len(packet_rows)} packets listed, {len(count)} in modules'
          if set(packet_rows) == set(count) else
          'only in packets.tsv: ' + examples(set(packet_rows) - set(count)) +
          '; only in modules: ' + examples(set(count) - set(packet_rows)))
    bad = [p for p in packet_rows if int(packet_rows[p]['modules']) != count.get(p, 0)]
    check('packets.modules_count', not bad,
          'modules column equals the actual module count for every packet'
          if not bad else 'wrong counts: ' + examples(bad))
    bad = [p for p in packet_rows
           if len(depths.get(p, set())) != 1 or int(packet_rows[p]['depth']) != next(iter(depths[p]))]
    check('packets.depth', not bad,
          'all modules in a packet share the depth listed in packets.tsv'
          if not bad else 'depth disagreements: ' + examples(bad))
    bad = [p for p in packet_rows if int(packet_rows[p]['open_in_snapshot']) != open_count.get(p, 0)]
    check('packets.open_in_snapshot', not bad,
          'open_in_snapshot equals the snapshot open count for every packet'
          if not bad else 'wrong open counts: ' + examples(bad))
    bad = [p for p in packet_rows if packet_rows[p]['first_module'] not in members.get(p, set())]
    check('packets.first_module', not bad,
          'first_module belongs to the packet for every packet'
          if not bad else 'bad first_module: ' + examples(bad))

    # ---- evidence/**/*.json ------------------------------------------------
    evidence_files = sorted((root / 'evidence').rglob('*.json'))
    check('evidence.present', len(evidence_files) > 0,
          f'{len(evidence_files)} JSON files under evidence/')
    unparsed, bad_tasks, bad_mathlib, bad_exit = [], [], [], []
    receipts = 0
    reviewed = 0
    missing_volunteer_tasks = []
    dup_reviewed, claim_mismatch, bad_objects, incomplete_reviewed = [], [], [], []
    sha256_re = re.compile(r'^[0-9a-f]{64}$')
    packet_task_ids = defaultdict(set)
    for r in rows:
        packet_task_ids[r['packet']].add(r['id'])
    for path in evidence_files:
        try:
            record = json.loads(path.read_text())
        except Exception as e:
            unparsed.append(f'{path.relative_to(root)}: {e}')
            continue
        rel_evidence = path.relative_to(root / 'evidence')
        in_volunteers = bool(rel_evidence.parts) and rel_evidence.parts[0] == 'volunteers'
        # Every file anywhere under evidence/volunteers/ is a reviewed build receipt
        # and must carry a non-empty string task list, a valid packet/module claim,
        # pinned mathlib commit, strict integer exit_code == 0, and SHA-256 object digests.
        if in_volunteers:
            if (not isinstance(record, dict)
                    or not isinstance(record.get('tasks'), list)
                    or not record['tasks']
                    or not all(isinstance(t, str) for t in record['tasks'])):
                missing_volunteer_tasks.append(str(rel_evidence))
            if isinstance(record, dict):
                if record.get('mathlib') != summary['mathlib_commit']:
                    bad_mathlib.append(str(rel_evidence))
                if type(record.get('exit_code')) is not int or record.get('exit_code') != 0:
                    bad_exit.append(str(rel_evidence))
                if 'packet' not in record:
                    incomplete_reviewed.append(str(rel_evidence))
        if isinstance(record, dict) and 'tasks' in record and isinstance(record['tasks'], list):
            receipts += 1
            tasks_list = record['tasks']
            all_str_tasks = all(isinstance(t, str) for t in tasks_list)
            bad_tasks += [f'{path.name}:{t}' for t in tasks_list if not isinstance(t, str) or t not in by_id]
            if not in_volunteers:
                if 'mathlib' in record and record['mathlib'] != summary['mathlib_commit']:
                    bad_mathlib.append(path.name)
                if 'exit_code' in record and (type(record['exit_code']) is not int or record['exit_code'] != 0):
                    bad_exit.append(path.name)
            # A reviewed receipt (one carrying a 'packet' claim: a packet id
            # for a packet build, or a module id for a single-module build)
            # credits work, so its task list has to be exactly what it
            # claims: no duplicates, every task in the claimed packet (or
            # the claimed module itself), one valid SHA-256 object digest per task,
            # and the full set of ids for a known claim.
            if 'packet' in record:
                reviewed += 1
                if not all_str_tasks:
                    continue
                claim = record['packet']
                tasks = tasks_list
                if len(set(tasks)) != len(tasks):
                    dup_reviewed.append(path.name)
                claim_mismatch += [f'{path.name}:{t}' for t in tasks
                                   if isinstance(t, str) and t in by_id and by_id[t]['packet'] != claim and t != claim]
                if isinstance(claim, str) and claim in packet_task_ids:
                    expected_tasks = packet_task_ids[claim]
                elif isinstance(claim, str) and claim in by_id:
                    expected_tasks = {claim}
                else:
                    expected_tasks = None
                if expected_tasks is None or not tasks or set(tasks) != expected_tasks:
                    incomplete_reviewed.append(path.name)
                objects = record.get('objects')
                if (not isinstance(objects, dict)
                        or set(objects) != set(tasks)
                        or not all(isinstance(v, str) and sha256_re.fullmatch(v) for v in objects.values())):
                    bad_objects.append(path.name)
    check('evidence.json_parses', not unparsed,
          'every evidence file parses as JSON' if not unparsed
          else 'unparseable: ' + examples(unparsed))
    check('evidence.receipt_tasks_exist', not bad_tasks,
          f'{receipts} receipts checked, every task id exists' if not bad_tasks
          else 'unknown task ids: ' + examples(bad_tasks))
    check('evidence.receipt_mathlib_pin', not bad_mathlib,
          'receipt mathlib matches summary.mathlib_commit where present' if not bad_mathlib
          else 'mathlib mismatch: ' + examples(bad_mathlib))
    check('evidence.receipt_exit_code', not bad_exit,
          'receipt exit_code is 0 where present' if not bad_exit
          else 'nonzero exit_code: ' + examples(bad_exit))
    check('evidence.reviewed_tasks_unique', not dup_reviewed,
          f'{reviewed} reviewed receipts checked, every task id appears once'
          if not dup_reviewed else 'duplicate task ids in: ' + examples(dup_reviewed))
    check('evidence.reviewed_tasks_match_claim', not claim_mismatch,
          'every reviewed task belongs to the claimed packet or is the claimed module'
          if not claim_mismatch else 'tasks outside the claim: ' + examples(claim_mismatch))
    check('evidence.reviewed_objects_match_tasks', not bad_objects,
          'object digest keys equal the task ids in every reviewed receipt'
          if not bad_objects else 'object keys differ from task ids in: ' + examples(bad_objects))
    check('evidence.volunteers_tasks_present', not missing_volunteer_tasks,
          'every file under evidence/volunteers/ carries a task list'
          if not missing_volunteer_tasks else 'volunteer receipts without a task list: ' + examples(missing_volunteer_tasks))
    check('evidence.reviewed_tasks_complete_claim', not incomplete_reviewed,
          'task ids equal the full set of ids for the claimed packet (or the claimed module itself) in every reviewed receipt'
          if not incomplete_reviewed else 'task ids do not equal the full set for the claim in: ' + examples(incomplete_reviewed))

    # ---- summary pins against verify.sh ------------------------------------
    verify = root / 'verify.sh'
    lean_pin = mathlib_pin = None
    if verify.exists():
        text = verify.read_text()
        m = re.search(r'leanprover/lean4:v(\d+\.\d+\.\d+)', text)
        lean_pin = m.group(1) if m else None
        m = re.search(r'mathlib rev-parse HEAD\)"\s*=\s*"([0-9a-f]{40})"', text)
        mathlib_pin = m.group(1) if m else None
    if lean_pin is None:
        print('SKIP summary.lean_pin -- no leanprover/lean4 pin found in verify.sh')
    else:
        check('summary.lean_pin', summary['lean'] == lean_pin,
              f"summary.lean={summary['lean']}, verify.sh pins {lean_pin}")
    if mathlib_pin is None:
        print('SKIP summary.mathlib_pin -- no mathlib commit found in verify.sh')
    else:
        check('summary.mathlib_pin', summary['mathlib_commit'] == mathlib_pin,
              f"summary.mathlib_commit={summary['mathlib_commit'][:12]}..., verify.sh pins {mathlib_pin[:12]}...")

    if failures:
        print(f'FAILED: {len(failures)} of {ran} checks failed: {", ".join(failures)}')
        return 1
    print(f'OK: all {ran} checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
