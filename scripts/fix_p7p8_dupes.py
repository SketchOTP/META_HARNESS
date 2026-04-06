import json, shutil
from datetime import datetime
from pathlib import Path

p = Path('.metaharness/memory/project_memory.json')
shutil.copy2(p, p.with_suffix('.json.bak3'))
mem = json.loads(p.read_text(encoding='utf-8'))
directives = mem['directives']

for dup_id in ['P007_auto', 'P008_auto']:
    indices = [i for i,d in enumerate(directives) if d['id']==dup_id and d['status']=='COMPLETED']
    print(dup_id, 'COMPLETED at', indices)
    if len(indices) >= 2:
        removed = directives.pop(indices[0])
        print('  Removed earlier ts:', removed['ts'])

traj = mem['metric_trajectory']
for dup_id in ['P007_auto', 'P008_auto']:
    indices = [i for i,t in enumerate(traj) if t[0]==dup_id]
    print('trajectory', dup_id, indices)
    if len(indices) >= 2:
        traj.pop(indices[0])
mem['metric_trajectory'] = traj

mem['total_cycles'] -= 2
mem['completed'] -= 2
mem['last_updated'] = datetime.utcnow().isoformat() + 'Z'
p.write_text(json.dumps(mem, indent=2, ensure_ascii=True), encoding='utf-8')
print('Done. total=' + str(mem['total_cycles']) + ' completed=' + str(mem['completed']))
