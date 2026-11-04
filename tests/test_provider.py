import base64
import json
import os
from pathlib import Path
import sys
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch

from research_runtime.provider import AmbiguousMutation, CreateRejected, FakeProvider, MCPProvider, PodNotFound, ProviderError


REQUEST = {"gpu_type": "NVIDIA A40", "gpu_count": 2, "volume_gb": 20,
           "container_disk_gb": 10, "image": "example/research:pinned"}


# A real stdio process exercises framing, initialization, discovery, and tool
# calls while all resource data and mutations remain inside this fake server.
SERVER = r'''
import json, sys
pod = {"id":"p1", "name":"aniket-test", "status":"RUNNING", "cost":2.0,
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
   rejection=args['body']['name'].removeprefix('aniket-reject-')
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
    def public_key(self):
        kind = b"ssh-ed25519"
        raw = struct.pack(">I", len(kind)) + kind + struct.pack(">I", 32) + bytes(range(32))
        return "ssh-ed25519 " + base64.b64encode(raw).decode() + " dedicated-worker"

    def test_owner_worker_public_key_is_injected_without_account_key_changes(self):
        key = self.public_key()
        provider = MCPProvider({"worker_public_key": key})
        pod = {"id": "p1", "name": "aniket-worker", "status": "RUNNING", "cost": 2,
               "disk": 10, "gpu": {"id": "NVIDIA A40", "count": 2},
               "mounts": {"persistent": {"path": "/workspace", "size": 20}}}
        with patch.object(provider, "_call", return_value=pod) as call:
            self.assertEqual(provider.create("aniket-worker", REQUEST)["id"], "p1")
            call.assert_called_once()
            name, args = call.call_args.args
            self.assertEqual(name, "create-pod")
            self.assertEqual(args["body"]["env"], {"PUBLIC_KEY": key})
        default = MCPProvider({})
        with patch.object(default, "_call", return_value=pod) as call:
            default.create("aniket-worker", REQUEST)
            self.assertNotIn("env", call.call_args.args[1]["body"])

    def test_worker_key_rejects_paths_private_material_and_malformed_public_keys_before_calls(self):
        values = (None, "", "/home/research/.ssh/id_ed25519", "-----BEGIN OPENSSH PRIVATE KEY-----",
                  self.public_key() + "\nssh-ed25519 another", "ssh-ed25519 not-base64",
                  "ssh-rsa " + self.public_key().split()[1], "ssh-ed25519 AAAABQ==")
        with patch("research_runtime.provider._StdioMCP") as network:
            for value in values:
                with self.subTest(value=type(value).__name__):
                    with self.assertRaisesRegex(ProviderError, "worker_public_key"):
                        MCPProvider({"worker_public_key": value})
            network.assert_not_called()

    def test_foreign_or_renamed_pods_never_receive_mutations(self):
        provider = MCPProvider({})
        provider.client = Mock()
        for name in ("tristan", "research-old", "Aniket-test", "", None):
            with self.subTest(name=name):
                provider.client.call.reset_mock()
                provider.client.call.return_value = {"id": "p1", "name": name}
                with self.assertRaises(ProviderError):
                    provider.create(name, REQUEST)
                provider.client.call.assert_not_called()
                with self.assertRaises(ProviderError):
                    provider.get("p1")
                for action in (provider.start, provider.stop):
                    provider.client.call.reset_mock()
                    with self.assertRaises(AmbiguousMutation):
                        action("p1")
                    self.assertEqual(provider.client.call.call_args_list,
                                     [unittest.mock.call("get-pod", {"id": "p1"})])

    def test_stop_checks_fresh_identity_without_requiring_a_price(self):
        provider = MCPProvider({})
        provider.client = Mock()
        owned = {"id": "p1", "name": "aniket-test", "status": "RUNNING"}
        stopped = {**owned, "status": "EXITED", "disk": 10, "cost": 0}
        provider.client.call.side_effect = [owned, {}, stopped]
        self.assertEqual(provider.stop("p1")["status"], "STOPPED")
        self.assertEqual(provider.client.call.call_args_list, [
            unittest.mock.call("get-pod", {"id": "p1"}),
            unittest.mock.call("pod-action", {"id": "p1", "body": {"action": "stop"}}),
            unittest.mock.call("get-pod", {"id": "p1"})])
        for metadata in ({"id": "wrong", "name": "aniket-test"},):
            provider.client.call.reset_mock()
            provider.client.call.side_effect = None
            provider.client.call.return_value = metadata
            with self.assertRaises(AmbiguousMutation):
                provider.stop("p1")
            self.assertEqual(provider.client.call.call_count, 1)

    def test_unavailable_name_check_never_starts_or_stops(self):
        for action in ("start", "stop"):
            provider = MCPProvider({})
            client = Mock()
            provider.client = client
            client.call.side_effect = ProviderError("offline")
            with self.assertRaises(AmbiguousMutation):
                getattr(provider, action)("p1")
            client.call.assert_called_once_with("get-pod", {"id": "p1"})

    def test_list_filters_foreign_pods_across_pages_before_pricing(self):
        provider = MCPProvider({})
        pages = [
            {"pods": [{"id": "foreign", "name": "tristan"}],
             "pagination": {"hasNextPage": True, "nextCursor": "next"}},
            {"pods": [{"id": "owned", "name": "aniket-intent", "status": "STARTING"}],
             "pagination": {"hasNextPage": False, "nextCursor": None}}]
        with patch.object(provider, "_call", side_effect=pages) as call:
            self.assertEqual([pod["id"] for pod in provider.list()], ["owned"])
            self.assertEqual(call.call_args.args[1]["cursor"], "next")

    def test_fake_provider_refuses_foreign_pods_and_detects_rename(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "provider.json"
            provider = FakeProvider({"fake_state": str(path)})
            with self.assertRaises(ProviderError):
                provider.create("tristan", REQUEST)
            owned = provider.create("aniket-test", REQUEST)
            state = json.loads(path.read_text())
            state["pods"][owned["id"]]["name"] = "tristan"
            path.write_text(json.dumps(state))
            before = path.read_text()
            for action in (provider.get, provider.start, provider.stop):
                with self.assertRaises(ProviderError):
                    action(owned["id"])
                self.assertEqual(path.read_text(), before)
            self.assertEqual(provider.list(), [])

    def test_stdio_lifecycle_and_conservative_price(self):
        config = {"command": [sys.executable, "-u", "-c", SERVER], "timeout_seconds": 2}
        with patch.dict(os.environ, {"RUNPOD_API_KEY": "fake-test-key"}):
            provider = MCPProvider(config)
            try:
                quote = provider.quote(REQUEST)
                self.assertGreater(quote["hourly_usd"], 2)
                self.assertEqual(quote["gpu_type"], "NVIDIA A40")
                pod = provider.create("aniket-test", REQUEST)
                self.assertEqual(pod["ssh"], {"host": "127.0.0.1", "port": 2222, "user": "root"})
                self.assertEqual(provider.list()[0]["id"], "p1")
                self.assertEqual(provider.stop("p1")["status"], "STOPPED")
                self.assertGreater(provider.start("p1")["hourly_usd"], 2)
                with self.assertRaises(PodNotFound) as missing:
                    provider.get("gone")
                self.assertEqual(missing.exception.pod_id, "gone")
                with self.assertRaises(CreateRejected) as rejected:
                    provider.create("aniket-reject-422", REQUEST)
                self.assertEqual(rejected.exception.status, 422)
                with self.assertRaises(AmbiguousMutation):
                    provider.create("aniket-reject-500", REQUEST)
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
                provider.create("aniket-test", REQUEST)
            self.assertEqual(error.exception.pod_id, "p1")
        with patch.object(provider, "_call", return_value={"pods": [{"id": "p1", "name": "aniket-intent", "status": "STARTING", "disk": 10}], "pagination": {"hasNextPage": False, "nextCursor": None}}):
            found = provider.list()[0]
            self.assertEqual(found["id"], "p1")
            self.assertIsNone(found["hourly_usd"])

    def test_mutation_transport_error_is_not_retried(self):
        provider = MCPProvider({})
        with patch.object(provider, "_call", side_effect=ProviderError("request timed out")) as call:
            with self.assertRaises(AmbiguousMutation):
                provider.create("aniket-test", REQUEST)
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
            pod = FakeProvider(config).create("aniket-test", REQUEST)
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
