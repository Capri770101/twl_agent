"""Quick application import verification with a failing exit status on errors."""
from importlib import import_module


CHECKS = (
    ('backend.config', (('backend.config', 'settings'), ('backend.config', 'setup_logging'))),
    ('backend.storage.db', (('backend.storage.db', 'init_db'),)),
    ('backend.storage.memory', (('backend.storage.memory', 'get_or_create_session'),)),
    ('backend.data_gateway (platform_db)', (
        ('backend.data_gateway.external', 'discover_external'),
        ('backend.data_gateway.external', 'query_external_entity'),
        ('backend.data_gateway.mapping_store', 'get_active_mapping'),
        ('backend.data_gateway.mapper', 'generate_mapping_draft'),
    )),
    ('backend.routers.chat', (('backend.routers.chat', 'router'),)),
    ('backend.routers.auth (multi-platform)', (
        ('backend.routers.auth', 'router'),
        ('backend.auth', 'require_platform_key'),
        ('backend.auth', 'derive_platform_user_id'),
    )),
    ('agent.agent', (('agent.agent', 'ReActAgent'),)),
)


def main() -> int:
    print('Checking dependencies...')
    failed = False
    for label, imports in CHECKS:
        try:
            for module_name, attribute in imports:
                getattr(import_module(module_name), attribute)
        except Exception as exc:
            print(f'[FAIL] {label}: {exc}')
            failed = True
        else:
            print(f'[OK] {label}')
    print('\nVerification complete!')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
