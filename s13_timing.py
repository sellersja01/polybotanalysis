import re
ages=[]
with open('/root/paper_s13.log') as f:
    for line in f:
        m = re.search(r'\[(\d\d):(\d\d):(\d\d)\] ENTRY', line)
        if m:
            h,mi,s = int(m.group(1)), int(m.group(2)), int(m.group(3))
            age = (mi % 5) * 60 + s
            ages.append(age)
print(f'Entries: {len(ages)}')
print(f'Avg age into candle: {sum(ages)/len(ages):.1f}s')
print(f'Median: {sorted(ages)[len(ages)//2]}s')
buckets = {'0-30s':0,'30-60s':0,'60-120s':0,'120-270s':0}
for a in ages:
    if a<=30: buckets['0-30s']+=1
    elif a<=60: buckets['30-60s']+=1
    elif a<=120: buckets['60-120s']+=1
    else: buckets['120-270s']+=1
for k,v in buckets.items(): print(f'  {k}: {v}')
