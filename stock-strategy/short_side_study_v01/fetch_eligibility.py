"""Public TWSE historical eligibility cache; no credentials or broker API."""
import hashlib
import json
from pathlib import Path
import urllib.request
import argparse

DATES=('20261002','20261005','20261006','20261008')


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('--dates',nargs='+',default=DATES)
    args=parser.parse_args(argv)
    destination=Path(__file__).resolve().parent/'sources'
    destination.mkdir(exist_ok=True)
    for date in args.dates:
        if len(date)!=8 or not date.isdigit():
            raise ValueError('date must be YYYYMMDD')
        if (destination/(date+'_twse_eligibility.json')).is_file():
            print(date,'CACHED',flush=True)
            continue
        url=f'https://www.twse.com.tw/exchangeReport/TWTB4U?date={date}&response=json'
        with urllib.request.urlopen(url,timeout=30) as response:
            payload=response.read()
        value=json.loads(payload)
        if value.get('stat')!='OK' or value.get('date')!=date:
            raise RuntimeError('Historical response date/status mismatch:'+date)
        table=next(t for t in value['tables'] if '暫停現股賣出後現款買進當沖註記' in t.get('fields',[]))
        fields=table['fields']; index=fields.index('暫停現股賣出後現款買進當沖註記')
        codes={row[0]:not bool(row[index].strip()) for row in table['data']}
        output={'session_date':date,'market':'TWSE','url':url,'sha256':hashlib.sha256(payload).hexdigest(),
                'codes':codes,'scope':'HISTORICAL_EXCHANGE_FLAG_ONLY_BROKER_ENTITLEMENT_UNPROVEN',
                'raw_response':value}
        (destination/(date+'_twse_eligibility.json')).write_text(json.dumps(output,ensure_ascii=False,sort_keys=True)+'\n')
        print(date,len(codes),flush=True)


if __name__=='__main__':
    main()
