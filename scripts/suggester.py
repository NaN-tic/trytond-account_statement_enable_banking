# This trytond-console script is used to test the success rate of the
# suggestions system of the account_statement_enable_banking module

from collections import defaultdict
from timer import Timer


pool = globals()['pool']
transaction = globals()['transaction']

Origin = pool.get('account.statement.origin')
Suggested = pool.get('account.statement.origin.suggested.line')
Line = pool.get('account.statement.line')
Invoice = pool.get('account.invoice')
Payment = pool.get('account.payment')
MoveLine = pool.get('account.move.line')

transaction.set_context(company=1)

def tuplify(line):
    x = line.related_to
    if isinstance(x, Payment) and x.line and isinstance(x.line.move_origin, Invoice):
        x = x.move_origin
    elif isinstance(x, MoveLine) and isinstance(x.move_origin, Invoice):
        x = x.move_origin
    return (str(line.account), str(line.party), line.amount,
        str(x))

t = Timer()
print('Searching...')
origins = Origin.search([
        ('state', '=', 'posted'),
        #('create_date', '>=', '2025-08-01'),
        ('create_date', '>=', '2025-08-05'),
        #('create_date', '>=', '2025-08-10'),
        ])

targets = {}
for origin in origins:
    targets[origin] = sorted([tuplify(x) for x in origin.lines])

print('Cancelling...', t)
with transaction.set_context(skip_warnings=True):
    Origin.cancel(origins)

print('Deleting lines...', t)
to_delete = sum([x.lines for x in origins], tuple())
Line.delete(to_delete)

print('Deleting suggested lines...', t)
to_delete = sum([x.suggested_lines for x in origins], tuple())
Suggested.delete(to_delete)

print('Registering...', t)
Origin.register(origins)

print('Searching suggestions...', t)
Origin.search_suggestions(origins)

print('Browsing origins...', t)
origins = Origin.browse(origins)

perfect = []
correct = defaultdict(list)
incorrect = []
for origin in origins:
    print(f'Origin {origin.id}: {origin.amount}')
    target_lines = targets[origin]
    tuplified = sorted([tuplify(line) for line in origin.lines])
    if tuplified == target_lines:
        perfect.append(origin)
        continue

    position = 0
    for suggested_line in origin.suggested_lines_tree:
        position += 1
        if suggested_line.childs:
            tuplified = sorted([tuplify(line) for line in suggested_line.childs])
        else:
            tuplified = [tuplify(suggested_line)]
        if tuplified == target_lines:
            correct[position].append(origin)
            break
    else:
        incorrect.append(origin)
        print('  Lines:')
        for target, line in zip(target_lines, tuplified):
            print(f'    {target} -> {line}')

percentage = len(perfect) / len(origins) * 100
print(f'Perfect: {len(perfect)}/{len(origins)} ({percentage:.2f}%)', t, [x.id for x in perfect])
for key in sorted(correct.keys()):
    values = correct[key]
    percentage = len(values) / len(origins) * 100
    print(f'Correct ({key}): {len(values)}/{len(origins)} ({percentage:.2f}%)', t, [x.id for x in values])
print(f'Incorrect: {len(incorrect)}/{len(origins)}', t, [x.id for x in incorrect])
