// Exact decimal strings shared by Web and native WeChat (no Number/BigInt runtime requirement).
export const PRECISION:Record<string,number>={CNY:2,USD:2,EUR:2,HKD:2,JPY:0,KWD:3}
export function minor(value:string,currency:string):string {
 const p=PRECISION[currency];if(p===undefined)throw Error('不支持的币种')
 const m=/^([+-]?)(\d+)(?:\.(\d+))?$/.exec(String(value).trim());if(!m)throw Error('金额格式无效')
 let frac=m[3]||'';if(frac.length>p){if(/[^0]/.test(frac.slice(p)))throw Error('金额超过币种精度');frac=frac.slice(0,p)}
 const n=(m[2]+frac.padEnd(p,'0')).replace(/^0+(?=\d)/,'');return m[1]==='-'&&n!=='0'?'-'+n:n
}
function add(a:string,b:string):string{
 const an=a.startsWith('-'),bn=b.startsWith('-');a=an?a.slice(1):a;b=bn?b.slice(1):b
 let neg=an;if(an!==bn&&(a.length<b.length||a.length===b.length&&a<b)){[a,b]=[b,a];neg=bn}
 const len=Math.max(a.length,b.length);a=a.padStart(len,'0');b=b.padStart(len,'0');let carry=0,out=''
 for(let i=len-1;i>=0;i--){let n=Number(a[i])+(an===bn?Number(b[i]):-Number(b[i]))+carry;if(an===bn){carry=n>=10?1:0;n%=10}else{carry=n<0?-1:0;if(n<0)n+=10}out=String(n)+out}
 if(carry>0)out='1'+out;out=out.replace(/^0+(?=\d)/,'');return neg&&out!=='0'?'-'+out:out
}
export function decimal(n:string,currency:string):string{const p=PRECISION[currency],neg=n.startsWith('-'),s=(neg?n.slice(1):n).padStart(p+1,'0');return (neg?'-':'')+(p?s.slice(0,-p)+'.'+s.slice(-p):s)}
export function sumMoney(values:string[],currency:string):string{return decimal(values.reduce((n,v)=>add(n,minor(v,currency)),'0'),currency)}
export function formatMoney(value:string,currency='CNY'):string{try{const s=decimal(minor(value,currency),currency),negative=s.startsWith('-'),parts=(negative?s.slice(1):s).split('.');const symbols:Record<string,string>={CNY:'¥',USD:'US$',EUR:'€',HKD:'HK$',JPY:'JP¥',KWD:'KWD '};return (negative?'-':'')+symbols[currency]+parts[0].replace(/\B(?=(\d{3})+(?!\d))/g,',')+(parts.length===2?'.'+parts[1]:'')}catch{return '—'}}
