import os
import hashlib
from collections import defaultdict

hashes = defaultdict(list)
for fn in sorted(os.listdir('extracted_screens')):
    if fn.endswith('.png'):
        path = os.path.join('extracted_screens', fn)
        with open(path, 'rb') as f:
            h = hashlib.md5(f.read()).hexdigest()
        hashes[h].append(fn)

print(f"Total unique designs: {len(hashes)}")
for h, fns in hashes.items():
    print(f"Hash {h[:8]} ({len(fns)} screens): {', '.join(fns[:3])}{'...' if len(fns) > 3 else ''}")
