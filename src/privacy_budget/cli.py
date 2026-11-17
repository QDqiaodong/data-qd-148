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

def run_influence(db,source):
    """Read-only privacy-unit influence audit over a SQLite microdata snapshot."""
    from .influence_api import audit_influence, finalize_report
    report=audit_influence(db,source)
    return finalize_report(report)

def _add_policy(parser):
    sub=parser.add_subparsers(dest='command')
    policy=sub.add_parser('policy',help='offline organisation policy compiler')
    policy.add_argument('--source',type=Path,required=True)
    policy.add_argument('--output',type=Path,required=True)

def _add_influence(parser):
    sub=parser.add_subparsers(dest='command')
    influence=sub.add_parser(
        'influence',
        help='read-only privacy-unit relation influence auditor',
    )
    influence.add_argument('--db',type=Path,required=True)
    influence.add_argument('--source',type=Path,required=True)
    influence.add_argument('--output',type=Path,required=True)

def _guard_paths(output,inputs):
    out=Path(output).resolve()
    for item in inputs:
        if Path(item).resolve()==out:
            raise ValueError('input and output must differ')
    target=Path(output)
    if target.is_symlink() or target.parent.is_symlink():
        raise ValueError('output cannot be a symlink')

def main(argv=None):
    # The legacy contract is ``--source S --output O`` with no subcommand;
    # ``policy`` and ``influence`` are the only new subcommands.
    raw=list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0]=='policy':
        parser=argparse.ArgumentParser(description=DESCRIPTION)
        _add_policy(parser)
        args=parser.parse_args(raw)
        try:
            _guard_paths(args.output,[args.source])
            report=run_policy(args.source);save(args.output,report)
        except (ValueError,TypeError,KeyError,OSError,OverflowError,RecursionError) as error:
            print(str(error),file=sys.stderr);return 2
        print(json.dumps(report,sort_keys=True,ensure_ascii=False,allow_nan=False));return 0
    if raw and raw[0]=='influence':
        parser=argparse.ArgumentParser(description=DESCRIPTION)
        _add_influence(parser)
        args=parser.parse_args(raw)
        try:
            _guard_paths(args.output,[args.source,args.db])
            if Path(args.db).resolve()==Path(args.source).resolve():
                raise ValueError('database and query input must differ')
            report=run_influence(args.db,args.source);save(args.output,report)
        except (ValueError,TypeError,KeyError,OSError,OverflowError,RecursionError) as error:
            print(str(error),file=sys.stderr);return 2
        print(json.dumps(report,sort_keys=True,ensure_ascii=False,allow_nan=False));return 0
    parser=argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(raw)
    try:
        _guard_paths(args.output,[args.source])
        report=run(args.source);save(args.output,report)
    except (ValueError,TypeError,KeyError,OSError,OverflowError,RecursionError) as error:
        print(str(error),file=sys.stderr);return 2
    print(json.dumps(report,sort_keys=True,ensure_ascii=False,allow_nan=False));return 0

if __name__=='__main__':raise SystemExit(main())
