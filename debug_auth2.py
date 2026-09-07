import sys
sys.path.insert(0, '/home/bacon/TC2-BaconBS-mesh')
from db_operations import get_ssh_credentials
from ssh_auth import verify_password

cred = get_ssh_credentials('arthur8734')
print('Credentials:', cred)

if cred:
    ok = verify_password('DontPanic42!', cred[1], cred[2])
    print('Verify:', ok)
else:
    print('No credentials found')
