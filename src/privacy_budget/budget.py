from collections import defaultdict

def compose(limits,reservations):
    ids=[r['id'] for r in reservations]
    if len(ids)!=len(set(ids)):raise ValueError('duplicate reservation ID')
    sums=defaultdict(lambda:[0,0]);items=defaultdict(list)
    for r in reservations:sums[r['key']][0]+=r['epsilon'];sums[r['key']][1]+=r['delta'];items[r['key']].append(r)
    output=[]
    for key,(max_e,max_d) in sorted(limits.items()):
        e,d=sums[key]
        if e>max_e or d>max_d:raise ValueError('composed privacy budget exceeded for '+key[0]+'/'+key[1])
        output.append({'dataset':key[0],'principal':key[1],'epsilon':str(e),'delta':str(d),'epsilon_limit':str(max_e),'delta_limit':str(max_d),'epsilon_remaining':str(max_e-e),'delta_remaining':str(max_d-d),'reservations':[{'id':r['id'],'epsilon':str(r['epsilon']),'delta':str(r['delta'])} for r in sorted(items[key],key=lambda r:r['id'])]})
    return {'scopes':output}
