DESCRIPTION='Compositional epsilon-delta budget accounting with atomic reservation checks'
import argparse, json, sys
from pathlib import Path
from .common import read, save
from .model import analyze
from .policy_model import PolicyError, compile_policy, parse_policy, violation_message

def run(source):
    data,digest=read(source)
    return {'schema_version':1,'source_sha256':digest,'result':analyze(data)}

def run_policy(source):
    """Compile an offline organisation policy; pure read, no state changes."""
    data,digest=read(source)
    scopes,rules=parse_policy(data)
    report=compile_policy(scopes,rules,digest)
    if report['status']!='ok':
        raise PolicyError(violation_message(report))
    return report

def _add_policy(parser):
    sub=parser.add_subparsers(dest='command')
    policy=sub.add_parser('policy',help='offline organisation policy compiler')
    policy.add_argument('--source',type=Path,required=True)
    policy.add_argument('--output',type=Path,required=True)

def main(argv=None):
    # The legacy contract is ``--source S --output O`` with no subcommand;
    # ``policy`` is the only new subcommand and keeps the same two flags.
    raw=list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0]=='policy':
        parser=argparse.ArgumentParser(description=DESCRIPTION)
        _add_policy(parser)
        args=parser.parse_args(raw)
        try:
            if args.source.resolve()==args.output.resolve():raise ValueError('input and output must differ')
            report=run_policy(args.source);save(args.output,report)
        except (ValueError,TypeError,KeyError,OSError,OverflowError,RecursionError) as error:
            print(str(error),file=sys.stderr);return 2
        print(json.dumps(report,sort_keys=True,ensure_ascii=False,allow_nan=False));return 0
    parser=argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(raw)
    try:
        if args.source.resolve()==args.output.resolve():raise ValueError('input and output must differ')
        report=run(args.source);save(args.output,report)
    except (ValueError,TypeError,KeyError,OSError,OverflowError,RecursionError) as error:
        print(str(error),file=sys.stderr);return 2
    print(json.dumps(report,sort_keys=True,ensure_ascii=False,allow_nan=False));return 0

if __name__=='__main__':raise SystemExit(main())
