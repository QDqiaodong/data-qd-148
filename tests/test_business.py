
import copy,json,unittest,tempfile,subprocess,sys,hashlib,random
from pathlib import Path
from fractions import Fraction
from privacy_budget.model import analyze
ROOT=Path(__file__).resolve().parents[1]
def sample():return json.loads((ROOT/'fixtures/input.json').read_text())
class Business(unittest.TestCase):
    def bad(self,d):
        with self.assertRaises((ValueError,TypeError)):analyze(d)
    def test_unknown_field(self):
        d=sample();d['extra']=1;self.bad(d)

    def test_compose(self):
        r=analyze(sample())['scopes'][0];self.assertEqual((r['epsilon'],r['delta'],r['epsilon_remaining'],r['delta_remaining']),('3/2','1/5000','1/2','1/1250'))
    def test_epsilon_exceeded(self):
        d=sample();d['reservations'].append({'id':'third','dataset':'mobility-week','principal':'analyst-a','epsilon':{'numerator':1,'denominator':1},'delta':{'numerator':0,'denominator':1}});self.bad(d)
    def test_delta_exceeded(self):
        d=sample();d['reservations']=[{'id':'x','dataset':'mobility-week','principal':'analyst-a','epsilon':{'numerator':1,'denominator':1},'delta':{'numerator':1,'denominator':500}}];self.bad(d)
    def test_zero_reservations(self):
        d=sample();d['reservations']=[];r=analyze(d)['scopes'][0];self.assertEqual((r['epsilon'],r['delta']),('0','0'))
    def test_unknown_scope(self):
        d=sample();d['reservations'][0]['dataset']='other';self.bad(d)
    def test_duplicate_id(self):
        d=sample();d['reservations'][1]['id']='count';self.bad(d)
    def test_fraction_normalization(self):
        d=sample();d['reservations']=[{'id':'x','dataset':'mobility-week','principal':'analyst-a','epsilon':{'numerator':2,'denominator':4},'delta':{'numerator':0,'denominator':1}}];self.assertEqual(analyze(d)['scopes'][0]['epsilon'],'1/2')
    def test_order(self):
        d=sample();d['reservations'].reverse();self.assertEqual(analyze(d),analyze(sample()))

class Command(unittest.TestCase):
    def call(self,source,output):
        return subprocess.run([sys.executable,'-m','privacy_budget.cli','--source',str(source),'--output',str(output)],capture_output=True,text=True)
    def test_cli_replay(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'out.json';src=ROOT/'fixtures/input.json';r=self.call(src,out);self.assertEqual(r.returncode,0,r.stderr);first=out.read_bytes();self.assertEqual(json.loads(first)['source_sha256'],hashlib.sha256(src.read_bytes()).hexdigest());self.assertEqual(self.call(src,out).returncode,0);self.assertEqual(first,out.read_bytes())
    def test_failure_preserves_output(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'bad.json';out=Path(td)/'out.json';out.write_text('prior')
            for body in ['{}','{"a":1,"a":2}','NaN']:
                src.write_text(body);self.assertEqual(self.call(src,out).returncode,2);self.assertEqual(out.read_text(),'prior')
    def test_output_safety(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'in.json';src.write_bytes((ROOT/'fixtures/input.json').read_bytes());before=src.read_bytes();self.assertEqual(self.call(src,src).returncode,2);self.assertEqual(src.read_bytes(),before);out=Path(td)/'link.json';out.symlink_to(src);self.assertEqual(self.call(src,out).returncode,2);self.assertEqual(src.read_bytes(),before)
