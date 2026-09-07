import sys
sys.path.insert(0, '/home/bacon/TC2-BaconBS-mesh')

# Simulate what the SSH server does
from ssh_auth import authenticate

# Test login for arthur8734
result = authenticate('arthur8734', 'DontPanic42!', 'fe80::7cf7:5bd9:8514:3abd')
print('Login result:', result)

# Test registration for a new user
result2 = authenticate('new:testuser999', 'DontPanic42!', 'fe80::7cf7:5bd9:8514:3abd')
print('Register result:', result2)

# Check rate limits
from db_operations import count_recent_link_attempts
count = count_recent_link_attempts('ssh-ip:fe80::7cf7:5bd9:8514:3abd', 'ssh_login')
print(f'Login attempts last hour: {count}')

count2 = count_recent_link_attempts('ssh-ip:fe80::7cf7:5bd9:8514:3abd', 'ssh_register')
print(f'Register attempts last hour: {count2}')
