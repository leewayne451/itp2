"""
Commonly used / expected passwords and base words (NIST SP 800-63B §3.1.1.2:
"compare against a blocklist of commonly-used, expected, or compromised values").

Matching is case-insensitive and also applies to the "core" word once leading
and trailing digits/symbols are stripped, so "Password2024!" and "!!qwerty12345"
are rejected too. The online breach check (Have I Been Pwned) covers the long
tail of leaked passwords; this local list works offline.
"""

COMMON = frozenset("""
123456 123456789 12345678 1234567890 1234567 12345 1234 123123 111111 000000 654321 666666 121212
112233 123321 987654321 qwerty qwertyuiop qwerty123 qwertyui asdfgh asdfghjkl zxcvbnm 1q2w3e4r
1q2w3e4r5t 1qaz2wsx qazwsx zaq12wsx password passw0rd p@ssw0rd p@ssword passwort motdepasse contrasena
password1 password12 password123 letmein welcome welcome1 welcome123 admin administrator admin123
root toor login guest user default changeme changeit secret master superman batman spiderman
iloveyou princess sunshine shadow monkey dragon football baseball soccer basketball hockey
michael jennifer jessica ashley charlie daniel thomas jordan hunter ranger buster tigger
trustno1 abc123 abcd1234 abcdef abcdefg abcdefgh aa123456 a123456 123abc qwe123 zxc123
starwars pokemon minecraft whatever freedom computer internet samsung apple google
microsoft linkedin facebook instagram twitter youtube hello hello123 lovely loveme
flower summer winter spring autumn monday friday sunday january december
singapore sgp merlion temasek nus ntu smu sit sutd
company employee manager director office staff team corporate business enterprise
servicejourney service journey access accessmanagement access123 itadmin itsupport helpdesk
hr humanresources finance payroll sales engineering approver requestor euss
qwerty1234 qwerty12345 password1234 password12345 1234qwer 12341234 11111111 00000000
88888888 99999999 123456789a 147258369 159753 147258 789456123 741852963
iloveyou1 letmein123 welcome1234 admin1234 administrator1 changeme123
correcthorsebatterystaple
""".split())
