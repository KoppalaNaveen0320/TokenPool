with open('full-ui.html', 'r', encoding='utf-8', errors='ignore') as f:
    text = f.read()

import re

# Check if image source is identical or different between card 9 and 10
p9_match = re.search(r'id="page-09-session-expired".*?<img class="page-thumb" src="data:image/png;base64,([^"]+)"', text, re.DOTALL)
p10_match = re.search(r'id="page-10-onboarding-role".*?<img class="page-thumb" src="data:image/png;base64,([^"]+)"', text, re.DOTALL)

if p9_match and p10_match:
    img9 = p9_match.group(1)
    img10 = p10_match.group(1)
    print("img9 len:", len(img9), "prefix:", img9[:30])
    print("img10 len:", len(img10), "prefix:", img10[:30])
    print("Are they identical?", img9 == img10)
