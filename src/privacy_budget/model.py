from fractions import Fraction
from .common import require,ident,number,rows
from .budget import compose

def fraction(v):
    require(v,'numerator denominator');n=number(v['numerator'],0,10**12);d=number(v['denominator'],1,10**12);return Fraction(n,d)

def analyze(data):
    require(data,'limits reservations');limits={};reservations=[]
    for l in rows(data['limits'],1000):
        require(l,'dataset principal epsilon delta');key=(ident(l['dataset']),ident(l['principal']))
        if key in limits:raise ValueError('duplicate budget limit')
        limits[key]=(fraction(l['epsilon']),fraction(l['delta']))
    for r in rows(data['reservations'],20000):
        require(r,'id dataset principal epsilon delta');rid=ident(r['id']);key=(ident(r['dataset']),ident(r['principal']))
        e=fraction(r['epsilon']);d=fraction(r['delta'])
        if e<=0 or d>=1:raise ValueError('each mechanism requires epsilon>0 and delta<1')
        reservations.append({'id':rid,'key':key,'epsilon':e,'delta':d})
    if any(r['key'] not in limits for r in reservations):raise ValueError('reservation has no declared budget')
    return compose(limits,reservations)
