"""Safe headless entrypoint; real projects/cloud accounts are not accepted."""
import argparse,json,tempfile
from pathlib import Path
from .controller import AppController
from .services import LocalWorkspace,MemoryDrive


def run_demo():
    with tempfile.TemporaryDirectory(prefix='cachecad-demo-') as root:
        Path(root,'local-part.sldprt').write_bytes(b'synthetic local CAD placeholder')
        remote=MemoryDrive({'remote-part.sldprt':b'synthetic remote CAD placeholder'})
        app=AppController(LocalWorkspace(root),remote)
        actions=app.analyze();results=app.execute()
        return {'mode':'offline-demo','reconstruction_version':'0.1.0',
                'actions':[{'id':a.action_id,'path':a.path,'kind':a.kind.value} for a in actions],
                'results':[{'id':r.action_id,'status':r.status.value,'detail':r.detail} for r in results],
                'phase':app.state.phase,'second_analysis_actions':len(app.analyze()),
                'network_access':False,'production_ready':False}

def main(argv=None):
    parser=argparse.ArgumentParser(description='CacheCAD independent reconstruction: offline workflow')
    parser.add_argument('--demo',action='store_true',help='Run only disposable synthetic files and an in-memory remote')
    parser.add_argument('--json',action='store_true',help='Print machine-readable demo results')
    args=parser.parse_args(argv)
    if not args.demo:parser.error('Only --demo is enabled; no real project/cloud access is supported')
    result=run_demo()
    print(json.dumps(result,indent=2) if args.json else 'Offline workflow finished: '+', '.join(x['status'] for x in result['results']))
    return 0

if __name__=='__main__':raise SystemExit(main())
