"""仅用于旧结构迁移fixture；不能用它降级真实协作数据库。"""


def remove_team_extensions(db):
    # 将当前临时库还原为旧fixture时，先移除schema14的依赖表，避免重复建表假失败。
    for table in ('auxiliary_legacy_bindings','auxiliary_resources','auxiliary_contributions','auxiliary_reports',
                  'auxiliary_executions','auxiliary_tasks','auxiliary_candidates','auxiliary_audits',
                  'auxiliary_sessions','auxiliary_configs','obligation_supersessions','task_continuations',
                  'recovery_applications','recovery_delivery_grants','recovery_reads','recovery_execution_attempts',
                  'recovery_requests','recovery_attempts','recovery_outbox','continuation_intents',
                  'control_event_cursors','control_scope_releases','control_releases','control_requests','recovery_policies'):
        db.execute('DROP TABLE IF EXISTS '+table)
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
