"""Exact paise allocation shared by receipt and serial valuation."""
from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal('0.01')
MONEY_FIELDS = ('basic_amount', 'discount', 'gst_amount', 'freight_cost',
                'freight_gst_amount', 'round_off', 'other_charges')


def money(value):
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def allocate(amount, weights):
    """Largest remainder; stable input order breaks ties, including negatives."""
    if not weights:
        if money(amount):
            raise ValueError('Cannot allocate a nonzero total to zero units.')
        return []
    weights = [max(Decimal(str(w)), Decimal(0)) for w in weights]
    total_weight = sum(weights)
    if not total_weight:
        weights = [Decimal(1)] * len(weights)
        total_weight = Decimal(len(weights))
    cents = int(money(amount) * 100)
    sign = -1 if cents < 0 else 1
    exact = [abs(cents) * w / total_weight for w in weights]
    shares = [int(v) for v in exact]
    remaining = abs(cents) - sum(shares)
    order = sorted(range(len(weights)), key=lambda i: (-(exact[i] - shares[i]), i))
    for index in order[:remaining]:
        shares[index] += 1
    return [Decimal(sign * share) / 100 for share in shares]


def unit_allocations(totals, quantity, **metadata):
    if quantity <= 0:
        raise ValueError('Quantity must be positive.')
    parts = {key: allocate(totals.get(key, 0), [1] * quantity) for key in MONEY_FIELDS}
    grand = money(totals.get('grand_total',
        money(totals.get('basic_amount')) - money(totals.get('discount'))
        + sum(money(totals.get(key)) for key in MONEY_FIELDS[2:])))
    if grand < 0:
        raise ValueError('Allocated purchase cost cannot be negative.')
    final = allocate(grand, [1] * quantity)
    units = []
    for index in range(quantity):
        row = {key: f'{values[index]:.2f}' for key, values in parts.items()}
        subtotal = parts['basic_amount'][index] - parts['discount'][index]
        subtotal += sum(parts[key][index] for key in MONEY_FIELDS[2:])
        row.update(metadata)
        row['rounding_adjustment'] = f'{final[index] - subtotal:.2f}'
        row['allocated_cost'] = f'{final[index]:.2f}'
        units.append(row)
    return units
