import requests

url = 'https://api.iiflcapital.com/v1/contractfiles/NSEFO.json'
print('Downloading contract file...')
resp = requests.get(url, timeout=60)
resp.raise_for_status()
data = resp.json()
items = data if isinstance(data, list) else data.get('data', data.get('instruments', []))

matches = []
for inst in items:
    if (inst.get('underlyingInstrumentSymbol') or '').upper() != 'NIFTY':
        continue
    strike = str(inst.get('strikePrice') or inst.get('strike') or '')
    expiry = inst.get('expiry') or inst.get('expiryDate') or ''
    ts = (inst.get('tradingSymbol') or inst.get('formattedInstrumentName') or '').upper()
    if strike == '25500' or ts.endswith('25500CE') or '17-02-2026' in expiry or '17-FEB-2026' in expiry.upper():
        matches.append(inst)

print('Found', len(matches), 'matches')
for m in matches[:20]:
    print(m.get('instrumentId'), '|', m.get('tradingSymbol'), '|', m.get('formattedInstrumentName'), '|', m.get('expiry'))
