/* Read the visible product DOM and embedded image data; never execute page data. */
globalThis.collectProductPage = async function(kind, requestedSkus, options) {
  const CAPTURE_VERSION='2.2';
  const subset=!!(options&&options.subset);
  const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  const txt=node=>(node?.innerText||node?.textContent||'').trim();
  const norm=s=>String(s||'').trim().toLowerCase().replace(/\s+/g,'');
  const http=value=>{if(!value||typeof value!=='string')return '';try{const u=new URL(value,location.href);return u.protocol==='https:'?u.href:''}catch{return ''}};
  // 列表里的款式缩略图常带 _sum / _400x400 这类后缀，去掉后缀才是原始大图。
  const full1688=value=>http(value)
    .replace(/\?[^#]*$/,'')
    .replace(/_(?:sum|q\d+|\d+x\d+(?:q\d+)?)\.(jpg|jpeg|png|webp)$/i,'.$1')
    .replace(/\.(?:jpg|jpeg|png)(?:_\d+x\d+[^?]*)/i,m=>m.split('_')[0]);
  const visible=node=>{const r=node?.getBoundingClientRect?.();return !!r&&r.width>4&&r.height>4};
  const variants=img=>{
    const candidates=[];
    for(const attr of ['data-old-hires','data-zoom-image','data-original','data-lazy-src','data-src','src']){
      const url=http(img.getAttribute(attr));if(url)candidates.push({url,area:attr==='data-old-hires'?1e12:0});
    }
    try{for(const [url,size] of Object.entries(JSON.parse(img.getAttribute('data-a-dynamic-image')||'{}')))candidates.push({url:http(url),area:size[0]*size[1]})}catch{}
    for(const entry of (img.srcset||'').split(',')){const [url,width]=entry.trim().split(/\s+/);if(url)candidates.push({url:http(url),area:parseInt(width)||0})}
    return candidates.filter(x=>x.url).sort((a,b)=>b.area-a.area).map(x=>x.url);
  };
  const verification=()=>{
    if(/验证码|拦截|安全检查|请完成验证|验证后继续访问/i.test(document.title))return true;
    if(/(x5secdata|punish)/i.test(location.href))return true;
    return [...document.querySelectorAll('.baxia-punish,#nc_1_n1z,#captchacharacters,iframe[src*="punish"],iframe[src*="captcha"],#baxia-dialog-content')].some(visible);
  };
  if(verification())throw Error('商品页需要先完成人工验证：浏览器已打开该页面，请拖动滑块或按提示验证，然后回工作台点“继续”。');
  const robot=/Robot Check|Enter the characters you see below/i.test(document.title)||!!document.querySelector('#captchacharacters');
  if(robot)throw Error('商品页出现验证码：请在已打开的页面完成验证，然后回工作台点“继续”。');
  const text=txt(document.body);
  if(kind==='amazon'){
    const title=txt(document.querySelector('#productTitle'));
    if(!title)throw Error('未读取到亚马逊商品标题。请在浏览器完成登录或验证后继续。');
    const bullets=[...document.querySelectorAll('#feature-bullets li .a-list-item')].map(txt).filter(x=>x&&!/Make sure this fits|See more product details/i.test(x)).slice(0,10);
    if(!bullets.length)throw Error('未读取到竞品五点描述，页面可能尚未加载完整。请展开 About this item 后继续。');
    const imageKey=value=>String(value||'').replace(/\._[^/]*_\./,'.').replace(/\?.*$/,'').toLowerCase();
    const gallery=[];              // 亚马逊自己公布的图库（主图+副图），顺序即页面顺序
    const seen=new Set();
    const push=(url,alternatives=[])=>{
      url=http(url);if(!url)return -1;
      const key=imageKey(url);
      const existing=gallery.findIndex(item=>imageKey(item.url)===key);
      if(existing>=0){
        // 同一张图换个尺寸后缀：只在分辨率更高时替换地址，不新增一张。
        for(const candidate of [url,...alternatives]){
          if(candidate&&!gallery[existing].alternatives.includes(candidate))gallery[existing].alternatives.push(candidate);
        }
        return existing;
      }
      if(seen.has(key))return -1;
      seen.add(key);
      gallery.push({url,alternatives:[...new Set([url,...alternatives].filter(Boolean))],name:'竞品主副图 '+(gallery.length+1)});
      return gallery.length-1;
    };
    // Amazon embeds the selected variation's gallery as data, usually under initial.
    for(const script of document.scripts){
      const body=script.textContent||'';
      if(!body.includes('colorImages'))continue;
      const start=/["']initial["']\s*:\s*\[/.exec(body);
      if(!start)continue;
      const offset=start.index+start[0].length-1;
      let depth=0,quote='',escaped=false,end=-1;
      for(let j=offset;j<body.length;j++){
        const c=body[j];
        if(quote){if(escaped)escaped=false;else if(c==='\\')escaped=true;else if(c===quote)quote='';continue}
        if(c==='"'||c==="'"){quote=c;continue}
        if(c==='[')depth++;
        if(c===']'&&--depth===0){end=j+1;break}
      }
      if(end<0)continue;
      try{for(const item of JSON.parse(body.slice(offset,end))){const main=Object.entries(item.main||{}).sort((a,b)=>(b[1][0]*b[1][1])-(a[1][0]*a[1][1])).map(x=>x[0]);const alternatives=[item.hiRes,...main,item.large].filter(Boolean);if(alternatives.length)push(alternatives[0],alternatives)}}catch{}
      if(gallery.length)break;
    }
    // 悬停缩略图只为拿到同一张图的高清地址，按缩略图序号对应到图库，不额外增加张数。
    const thumbs=[...document.querySelectorAll('#altImages li:not(.videoThumbnail) img')].filter(img=>!(/video|360/i.test(img.alt||'')));
    for(const [index,thumb] of thumbs.slice(0,12).entries()){
      thumb.dispatchEvent(new MouseEvent('mouseover',{bubbles:true}));
      thumb.closest('li')?.dispatchEvent(new MouseEvent('mouseenter',{bubbles:false}));
      await pause(320);
      const hero=document.querySelector('#landingImage,#imgBlkFront');
      if(!hero)continue;
      const candidates=variants(hero);
      if(!candidates.length)continue;
      if(gallery.length)push(candidates[0],candidates);
      else if(index===gallery.length)push(candidates[0],candidates);
    }
    if(!gallery.length)throw Error('未读取到竞品主副图，已停止，避免使用推荐区图片。');
    return {kind,source:location.href,title,page_title:document.title,bullets,images:gallery.slice(0,12),gallery_count:gallery.length,text:bullets.join('\n'),capture_version:CAPTURE_VERSION,mode:'gallery',captured_at:new Date().toISOString()};
  }
  const title=txt(document.querySelector('h1'))||document.title;
  // 拿货商品的主图 + 副图：没有款式区分时用它们当基准图。
  const galleryImages=()=>{
    const out=[],seen=new Set();
    const add=(raw,label)=>{
      const url=full1688(raw);if(!url||seen.has(url))return;seen.add(url);
      out.push({url,alternatives:[url],name:label||('拿货主副图 '+(out.length+1)),matched:true,source:'gallery'});
    };
    const html=document.documentElement.innerHTML||'';
    for(const match of html.matchAll(/"(?:imageUrl|fullPathImageURI|imageURL)"\s*:\s*"(https?:[^"]+?\.(?:jpg|jpeg|png|webp)[^"]*)"/gi)){
      add(match[1].replace(/\\\//g,'/'));
    }
    for(const img of document.querySelectorAll('[class*="gallery"] img,[class*="Gallery"] img,[class*="main"] img,[class*="Main"] img,[class*="detail"] img,[class*="Detail"] img,[class*="od-"] img')){
      add(img.getAttribute('data-src')||img.getAttribute('data-lazy-src')||img.src);
    }
    return out.slice(0,8);
  };
  const SKU_SELECTORS=['[class*="sku"],[class*="Sku"],[class*="SKU"]','[class*="prop"],[class*="Prop"]','[data-testid*="sku"],[data-spm*="sku"]'];
  const findRoots=()=>{
    const roots=[];
    for(const selector of SKU_SELECTORS){
      for(const node of document.querySelectorAll(selector)){
        if(node.querySelector('img'))roots.push(node);
      }
    }
    return roots;
  };
  let skuRoots=[];
  for(let round=0;round<12;round++){
    if(verification())throw Error('商品页需要先完成人工验证：浏览器已打开该页面，请拖动滑块或按提示验证，然后回工作台点“继续”。');
    skuRoots=findRoots();
    if(skuRoots.length)break;
    await pause(1200);
    try{window.scrollTo(0,Math.min(document.body.scrollHeight,1200))}catch{}
  }
  if(!skuRoots.length){
    const login=/登录|login|passport/i.test(location.href)||/请登录|立即登录|会员登录|扫码登录/i.test(text.slice(0,4000));
    if(login)throw Error('当前页面要求登录1688：请在已打开的标签页登录后，回工作台点“继续”。');
    const gallery=galleryImages();
    if(gallery.length){
      return {kind:'supplier',source:location.href,title,page_title:document.title,text:'',images:gallery,skus:[],capture_version:CAPTURE_VERSION,mode:'gallery',captured_at:new Date().toISOString()};
    }
    throw Error('没有读到1688的款式区域，也没读到主副图（页面可能没加载完或结构已变化）。请在浏览器里确认能看到商品图片后点“继续”。');
  }
  const candidates=[];
  for(const root of skuRoots){
    for(const img of root.querySelectorAll('img')){
      let node=img;
      for(let level=0;level<4&&node;level++,node=node.parentElement){
        const name=txt(node);
        for(const label of [img.alt,node.getAttribute('title'),node.getAttribute('data-title'),name]){
          if(label&&label.length<=80&&node.querySelectorAll('img').length<=1){
            candidates.push({label:label.trim(),node,img});
          }
        }
      }
    }
  }
  const requested=(requestedSkus||[]).filter(Boolean);
  let selected=[];
  const uniqueCandidates=[...new Map(candidates.map(c=>[full1688(c.img.getAttribute('data-src')||c.img.src),c])).values()];
  // 宽松比较：忽略括号、空格、单位符号，页面写“圣诞树-10个装”也能对上资料里的“圣诞树”。
  const loose=s=>String(s||'').toLowerCase().replace(/[\s\-_*×✕:：()（）\[\]【】,，、.。;；/]/g,'');
  if(requested.length){
    const missed=[];
    for(const sku of requested){
      let unique=[...new Map(candidates.filter(c=>norm(c.label)===norm(sku))
        .map(c=>[full1688(c.img.getAttribute('data-src')||c.img.src),c])).values()];
      if(unique.length!==1&&loose(sku)){
        const target=loose(sku);
        unique=[...new Map(candidates.filter(c=>{
          const label=loose(c.label);
          return label&&(label.includes(target)||target.includes(label));
        }).map(c=>[full1688(c.img.getAttribute('data-src')||c.img.src),c])).values()];
      }
      if(unique.length!==1){
        missed.push(sku);
        continue;
      }
      selected.push({...unique[0],label:sku});
    }
    const labels=[...new Set(candidates.map(c=>c.label).filter(Boolean))].slice(0,20).join('、');
    if(!selected.length){
      // 一个都没匹配上：多半是这个链接只有单一款式，用它的主副图当基准图。
      const gallery=galleryImages();
      if(uniqueCandidates.length<=1||!subset){
        if(gallery.length){
          return {kind:'supplier',source:location.href,title,page_title:document.title,text:'',images:gallery,skus:[],capture_version:CAPTURE_VERSION,mode:'gallery',captured_at:new Date().toISOString()};
        }
      }
      throw Error('这个链接里没有资料中写的款式。页面上的款式名是：'+(labels||'（没读到名称）')+'。请核对拿货规格，或改用主副图（该链接只有单款时会自动用主副图）。');
    }
    if(missed.length&&!subset&&uniqueCandidates.length>1){
      throw Error('拿货规格里的款式“'+missed[0]+'”没有唯一匹配到1688款式。页面上的款式名是：'+(labels||'（没读到名称）')+'。请核对资料里的拿货规格文字，或在1688页面展开该款式后点“继续”。');
    }
  }else{
    if(uniqueCandidates.length!==1){
      const gallery=galleryImages();
      if(uniqueCandidates.length<=1&&gallery.length){
        return {kind:'supplier',source:location.href,title,page_title:document.title,text:'',images:gallery,skus:[],capture_version:CAPTURE_VERSION,mode:'gallery',captured_at:new Date().toISOString()};
      }
      const labels=[...new Set(candidates.map(c=>c.label).filter(Boolean))].slice(0,20).join('、');
      throw Error('1688这个商品有多个款式，资料里没写清楚要哪款。请在资料里的“拿货规格”写清楚款式名，或在1688页面只留一个款式（页面上的款式名：'+(labels||'（没读到名称）')+'）。');
    }
    selected=uniqueCandidates;
  }
  const images=[];
  for(const selectedSku of selected){
    // The SKU thumbnail itself is a labelled identity source. Request its original
    // CDN asset rather than selecting an unrelated gallery photo after a click.
    const candidates=variants(selectedSku.img);const original=full1688(candidates[0]||'');
    if(!original)throw Error('SKU '+selectedSku.label+' 缺少产品图片');
    images.push({url:original,alternatives:[...new Set([original,...candidates])],name:selectedSku.label,sku:selectedSku.label,matched:true});
  }
  const attributes=[...document.querySelectorAll('[class*="attribute"],[class*="Attribute"],[class*="product-prop"]')].map(txt).filter(x=>x.length<6000).join('\n').slice(0,9000);
  return {kind:'supplier',source:location.href,title,page_title:document.title,text:attributes,images,skus:images.map(x=>x.sku),capture_version:CAPTURE_VERSION,mode:'sku',captured_at:new Date().toISOString()};
};
