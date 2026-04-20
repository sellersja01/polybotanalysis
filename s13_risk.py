import re
costs=[]
with open('/root/paper_s13.log') as f:
    for line in f:
        m = re.search(r'ENTRY \[\w+\] (Up|Down) @(\d\.\d+)', line)
        if m:
            costs.append(float(m.group(2)) * 100)
print(f'Entries: {len(costs)}')
print(f'Avg $/entry: ${sum(costs)/len(costs):.2f}')
print(f'Min: ${min(costs):.2f}  Max: ${max(costs):.2f}')
print(f'Total capital deployed: ${sum(costs):.2f}')
