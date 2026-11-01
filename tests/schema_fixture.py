"""仅用于旧结构迁移fixture；不能用它降级真实协作数据库。"""


def remove_team_extensions(db):
    for table in ('member_context_reads','managed_permissions','repair_links','review_issues','publications','review_stages',
                  'pool_backoff','reserve_proposals','resource_locks','invitations','member_history','requirement_changes'):
        db.execute('DROP TABLE IF EXISTS '+table)
    for table,columns in {
        'members':('binding_version','lifecycle','online','verified','capabilities'),
        'messages':('recipient_version','sender_version'),
        'tasks':('task_kind','requirements_version','priority','capabilities','resources','review_required','acceptance','required_checks')
    }.items():
        existing={r[1] for r in db.execute('PRAGMA table_info('+table+')')}
        for column in columns:
            if column in existing:db.execute('ALTER TABLE '+table+' DROP COLUMN '+column)
