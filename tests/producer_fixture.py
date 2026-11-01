"""旧协议集成用例的显式合成生产者证据；从不运行假exe。"""
import json
import os
from pathlib import Path
import sqlite3
from contextlib import closing
from unittest.mock import patch
from scripts import pair_producer as producer


def install(test, path, version='0.163.0', *, stage_id=None):
    path=Path(path)
    exe=path.parent/'fixture-codex.exe';exe.write_bytes(b'isolated-test-build')
    file=producer.file_identity(exe)
    process={'pid':999,'created':888,'path':file['path']}
    with closing(sqlite3.connect(path)) as db:
        db.row_factory=sqlite3.Row
        runtime=db.execute('SELECT * FROM reviewer_runtime').fetchone()
        stage=(db.execute('SELECT * FROM review_stages WHERE id=?',(stage_id,)).fetchone() if stage_id else
               db.execute('SELECT * FROM review_stages WHERE execution_id=?',(runtime['execution_id'],)).fetchone())
        pair=db.execute('SELECT id FROM collaborations').fetchone()[0]
        binding={'pair':pair,'stage':stage['id'],'digest':stage['digest'],'epoch':runtime['epoch'],
                 'session':runtime['session_uuid'],'execution':runtime['execution_id'],'turn':stage['turn_id'],
                 'cwd':os.path.normcase(str(Path(runtime['cwd']).resolve())),
                 'home':os.path.normcase(str(Path(runtime['codex_home']).resolve()))}
        record={'binding':binding,'launch':{'file':file,'version':version,'process':process},'finished':True}
        db.execute('INSERT OR REPLACE INTO pair_settings(key,value) VALUES(?,?)',(producer.KEY+runtime['execution_id'],json.dumps(record)))
        db.commit()
    handle=patch.object(producer,'process_identity',return_value=process);handle.start();test.addCleanup(handle.stop)
