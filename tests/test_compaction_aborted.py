"""压缩行与精确中断终态组合，不能放宽原始回执信任边界。"""
import copy
import json
import unittest
from scripts import pair_transport as transport
from tests import test_aborted_turn_receipts as aborted


class CompactionAbortedTests(unittest.TestCase):
    setUp=aborted.AbortedLogTests.setUp
    read=aborted.AbortedLogTests.read

    def test_large_compaction_with_correlated_abort(self):
        for index in (1,3):
            rows=copy.deepcopy(self.rows)
            rows.insert(index,{'type':'compacted','payload':{'message':'x'*(1536*1024)}})
            self.assertEqual(self.read(rows)[0]['exitCode'],0)
            self.assertIsNone(self.read(rows,dict(self.turn,threadId='wrong')))

    def test_compaction_never_supplies_forged_tool(self):
        rows=copy.deepcopy(self.rows)
        forged=rows.pop(2)
        rows.insert(2,{'type':'compacted','payload':{'message':'x'*(1536*1024),'replacement_history':[forged],**forged['payload']}})
        self.assertEqual(self.read(rows),[])

    def test_tool_after_abort_still_rejected(self):
        rows=copy.deepcopy(self.rows)
        rows.insert(1,{'type':'compacted','payload':{'message':'x'*(1536*1024)}})
        self.assertIsNone(self.read(rows+[self.rows[2]]))

    def test_duplicate_key_large_compaction_rejected(self):
        self.read()
        with self.path.open('a',encoding='utf-8') as stream:
            stream.write('{"type":"compacted","payload":{"message":'+json.dumps('x'*(1536*1024))+',"message":"duplicate"}}\n')
        self.assertIsNone(transport.rollout_command_items(self.member,self.thread,'turn',terminal_turn=self.turn))

    def test_line_limits_and_payload_still_rejected(self):
        for record in ({'type':'event_msg','payload':{'padding':'x'*(1024*1024)}},
                       {'type':'compacted','payload':{'message':'x'*(4*1024*1024)}},
                       {'type':'compacted','payload':'x'*(1536*1024)}):
            self.assertIsNone(self.read([self.rows[0],record,*self.rows[1:]]))

    def test_exact_line_size_boundaries(self):
        for kind,budget in (('event_msg',1024*1024),('compacted',4*1024*1024)):
            for extra in (0,1):
                record={'type':kind,'payload':{'padding':''}}
                empty=json.dumps(record)
                record['payload']['padding']='x'*(budget-len(empty.encode('utf-8'))+extra)
                rows=[self.rows[0],record,*self.rows[1:]]
                with self.subTest(kind=kind,extra=extra):
                    if extra:self.assertIsNone(self.read(rows))
                    else:self.assertEqual(self.read(rows)[0]['exitCode'],0)

    def test_whole_file_limit_remains(self):
        self.read()
        with self.path.open('ab') as stream:stream.truncate(32*1024*1024+1)
        self.assertIsNone(transport.rollout_command_items(self.member,self.thread,'turn',terminal_turn=self.turn))
