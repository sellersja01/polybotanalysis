import re
wins=[]; losses=[]; up_entries=[]; dn_entries=[]
with open('/root/paper_s13.log') as f:
    for line in f:
        m = re.search(r'([WL]) \[\w+\] (Up|Down) @(\d\.\d+) pnl=\$([+-][\d.]+)', line)
        if m:
            tag, side, ask, pnl = m.group(1), m.group(2), float(m.group(3)), float(m.group(4))
            (wins if tag=='W' else losses).append(pnl)
            (up_entries if side=='Up' else dn_entries).append(ask)
print(f'Wins: {len(wins)}  avg=${sum(wins)/len(wins):+.2f}  total=${sum(wins):+.2f}')
print(f'Losses: {len(losses)}  avg=${sum(losses)/len(losses):+.2f}  total=${sum(losses):+.2f}')
print(f'Up entries: {len(up_entries)}  avg ask={sum(up_entries)/len(up_entries):.3f}')
print(f'Down entries: {len(dn_entries)}  avg ask={sum(dn_entries)/len(dn_entries):.3f}')
