import os

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'change-this-secret-key')

    # MySQL (OrangeHRM same DB or separate)
    DB_HOST = os.environ.get('DB_HOST', 'localhost')
    DB_USER = os.environ.get('DB_USER', 'orangehrm')
    DB_PASSWORD = os.environ.get('DB_PASSWORD', '')
    DB_NAME = os.environ.get('DB_NAME', 'orangehrm')

    # Keycloak OIDC
    KEYCLOAK_CLIENT_ID = os.environ.get('KEYCLOAK_CLIENT_ID', 'leave4day')
    KEYCLOAK_CLIENT_SECRET = os.environ.get('KEYCLOAK_CLIENT_SECRET', '')
    KEYCLOAK_META_URL = os.environ.get(
        'KEYCLOAK_META_URL',
        'http://your-keycloak-server/realms/your-realm/.well-known/openid-configuration'
    )

    # API key for AppScript imports
    IMPORT_API_KEY = os.environ.get('IMPORT_API_KEY', 'change-this-api-key')