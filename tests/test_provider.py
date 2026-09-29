import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from research_runtime.provider import AmbiguousMutation, CreateRejected, FakeProvider, MCPProvider, PodNotFound, ProviderError


REQUEST = {"gpu_type": "NVIDIA A40", "gpu_count": 2, "volume_gb": 20,
           "container_disk_gb": 10, "image": "example/research:pinned"}


# A real stdio process exercises framing, initialization, discovery, and tool
# calls while all resource data and mutations remain inside this fake server.
SERVER = r'''
import json, sys
pod = {"id":"p1", "name":"test", "status":"RUNNING", "cost":2.0,
       "gpu":{"id":"NVIDIA A40","count":2}, "disk":10,
       "mounts":{"persistent":{"path":"/workspace","size":20}},
       "ssh":{"direct":{"host":"127.0.0.1","port":2222,"username":"root"}}}
schemas = {"list-gpu-types": ([], ["include","product","cloud","count"]),
           "create-pod": (["body"], ["body"]), "get-pod": (["id"], ["id"]),
           "list-pods": ([], ["limit","cursor"]), "pod-action": (["id","body"], ["id","body"])}
for line in sys.stdin:
 msg=json.loads(line); method=msg['method']
 if 'id' not in msg: continue
 if method=='initialize': out={'protocolVersion':'2024-11-05','capabilities':{},'serverInfo':{'name':'fake','version':'1'}}
 elif method=='tools/list': out={'tools':[{'name':n,'inputSchema':{'type':'object','required':r,'properties':{k:{} for k in p}}} for n,(r,p) in schemas.items()]}
 else:
  name=msg['params']['name']; args=msg['params']['arguments']
  if name=='list-gpu-types':
   payload={'gpus':[{'id':'NVIDIA A40','name':'A40','availability':'HIGH','maxCount':{'secure':8},'price':{'secure':1.0}}]}
  elif name=='create-pod':
   assert args['body']['mounts']['persistent']['size']==20
   assert args['body']['gpu']['count']==2
   rejection=args['body']['name'].removeprefix('reject-')
   payload={'status':int(rejection),'title':'Rejected'} if rejection.isdigit() else pod
  elif name=='pod-action':
   assert args['body']['action'] in ['start','stop']
   pod['status']='EXITED' if args['body']['action']=='stop' else 'RUNNING'
   pod['cost']=0 if pod['status']=='EXITED' else 2
   payload={}
  elif name=='list-pods': payload={'pods':[pod], 'pagination':{'hasNextPage':False,'nextCursor':None}}
  else: payload={'status':404,'title':'Not Found','detail':'pod not found'} if args['id']=='gone' else pod
  out={'content':[{'type':'text','text':json.dumps(payload)}], 'isError':type(payload.get('status')) is int and payload['status']>=400}
 print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':out}),flush=True)
'''


class ProviderTests(unittest.TestCase):
    def test_stdio_lifecycle_and_conservative_price(self):
        config = {"command": [sys.executable, "-u", "-c", SERVER], "timeout_seconds": 2}
        with patch.dict(os.environ, {"RUNPOD_API_KEY": "fake-test-key"}):
            provider = MCPProvider(config)
            try:
                quote = provider.quote(REQUEST)
                self.assertGreater(quote["hourly_usd"], 2)
                self.assertEqual(quote["gpu_type"], "NVIDIA A40")
                pod = provider.create("test", REQUEST)
                self.assertEqual(pod["ssh"], {"host": "127.0.0.1", "port": 2222, "user": "root"})
                self.assertEqual(provider.list()[0]["id"], "p1")
                self.assertEqual(provider.stop("p1")["status"], "STOPPED")
                self.assertGreater(provider.start("p1")["hourly_usd"], 2)
                with self.assertRaises(PodNotFound) as missing:
                    provider.get("gone")
                self.assertEqual(missing.exception.pod_id, "gone")
                with self.assertRaises(CreateRejected) as rejected:
                    provider.create("reject-422", REQUEST)
                self.assertEqual(rejected.exception.status, 422)
                with self.assertRaises(AmbiguousMutation):
                    provider.create("reject-500", REQUEST)
            finally:
                provider.close()

    def test_unknown_running_rate_is_not_zero(self):
        provider = MCPProvider({})
        with self.assertRaisesRegex(ProviderError, "Only start and stop"):
            provider._call("pod-action", {"id": "p1", "body": {"action": "terminate"}})
        with self.assertRaises(ProviderError):
            provider._normalize({"id": "p1", "status": "RUNNING", "disk": 10})
        with patch.object(provider, "_call", return_value={"id": "p1", "status": "STARTING", "disk": 10}):
            with self.assertRaises(AmbiguousMutation) as error:
                provider.create("test", REQUEST)
            self.assertEqual(error.exception.pod_id, "p1")
        with patch.object(provider, "_call", return_value={"pods": [{"id": "p1", "name": "intent", "status": "STARTING", "disk": 10}], "pagination": {"hasNextPage": False, "nextCursor": None}}):
            found = provider.list()[0]
            self.assertEqual(found["id"], "p1")
            self.assertIsNone(found["hourly_usd"])

    def test_mutation_transport_error_is_not_retried(self):
        provider = MCPProvider({})
        with patch.object(provider, "_call", side_effect=ProviderError("request timed out")) as call:
            with self.assertRaises(AmbiguousMutation):
                provider.create("test", REQUEST)
            self.assertEqual(call.call_count, 1)

    def test_server_timeout_is_bounded_and_process_closed(self):
        config = {"command": [sys.executable, "-u", "-c", "import time; time.sleep(10)"],
                  "timeout_seconds": 0.1}
        with patch.dict(os.environ, {"RUNPOD_API_KEY": "fake-test-key"}):
            with self.assertRaisesRegex(ProviderError, "timed out"):
                MCPProvider(config).quote(REQUEST)

    def test_fake_state_survives_new_instances_and_stop_retains_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {"fake_state": str(Path(tmp) / "provider.json")}
            pod = FakeProvider(config).create("test", REQUEST)
            second = FakeProvider(config)
            second.stop(pod["id"])
            self.assertEqual(second.get(pod["id"])["status"], "STOPPED")
            self.assertEqual(second.get(pod["id"])["volume_gb"], 20)
            self.assertEqual(len(second.list()), 1)
            self.assertGreater(second.start(pod["id"])["hourly_usd"], 2)

    def test_private_env_file_is_data_not_a_shell_program(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runpod.env"
            path.write_text("RUNPOD_API_KEY=fake-test-key\n")
            path.chmod(0o644)
            config = {"env_file": str(path), "command": [sys.executable, "-u", "-c", SERVER]}
            with self.assertRaisesRegex(ProviderError, "mode 600"):
                MCPProvider(config).quote(REQUEST)
            path.chmod(0o600)
            path.write_text("RUNPOD_API_KEY=fake-test-key\nUNEXPECTED=value\n")
            with self.assertRaisesRegex(ProviderError, "only RUNPOD_API_KEY"):
                MCPProvider(config).quote(REQUEST)


if __name__ == "__main__":
    unittest.main()
