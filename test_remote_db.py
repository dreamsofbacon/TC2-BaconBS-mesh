import sys
sys.path.insert(0, '/home/bacon/TC2-BaconBS-mesh')
import db_operations

result = db_operations.alias_owner('arthur8734')
print('alias_owner result:', result)

cred = db_operations.get_ssh_credentials('arthur8734')
print('ssh_credentials:', cred)

from ssh_auth import verify_password
if cred:
    ok = verify_password('DontPanic42!', cred[1], cred[2])
    print('Password verify:', ok)
