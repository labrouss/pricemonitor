# Examine the current candidate set to understand the 17k
from db import get_store
import dedup, collections
s = get_store()
rows = s.export_candidates(status='pending')
print('total pending:', len(rows))
# score distribution
buckets = collections.Counter(round(float(r['score']),1) for r in rows)
print('score distribution:', dict(sorted(buckets.items())))
# how many are CROSS-chain (the valuable ones) vs same-chain?
cross = same = 0
for r in rows:
    ar = set((r.get('a_retailers') or '').split(','))
    br = set((r.get('b_retailers') or '').split(','))
    if ar & br and ar==br: same += 1
    else: cross += 1
print('cross-retailer pairs:', cross, '| same-retailer pairs:', same)
# sample 8 high and 8 low
rows.sort(key=lambda r: -float(r['score']))
print('\n--- HIGHEST 6 ---')
for r in rows[:6]: print(f"  {r['score']} | {r['a_name'][:40]} <> {r['b_name'][:40]}")
print('\n--- LOWEST 6 ---')
for r in rows[-6:]: print(f"  {r['score']} | {r['a_name'][:40]} <> {r['b_name'][:40]}")
