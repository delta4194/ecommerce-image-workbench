document.getElementById('collect').onclick=async()=>{
const status=document.getElementById('status');
try{
 const [tab]=await chrome.tabs.query({active:true,currentWindow:true});
 const host=new URL(tab.url).hostname;
 if(!/(^|\.)1688\.com$/.test(host)&&!/(^|\.)amazon\.(com|de|fr|it|es|nl|se|pl|co\.uk|co\.jp|com\.au)$/.test(host))throw Error('请在1688或亚马逊商品页使用');
 const [{result}]=await chrome.scripting.executeScript({target:{tabId:tab.id},func:()=>{
   const images=[],seen=new Set();
   const add=(url,name)=>{try{url=new URL(url,location.href).href;if(url.startsWith('https://')&&!seen.has(url)){seen.add(url);images.push({url,name:(name||'图片候选').trim().slice(0,100)})}}catch{}};
   document.querySelectorAll('img').forEach(img=>{
     const rect=img.getBoundingClientRect();
     if((img.naturalWidth<100||img.naturalHeight<100)&&(rect.width<60||rect.height<60))return;
     const name=img.alt||img.closest('[title]')?.getAttribute('title')||img.parentElement?.innerText||'';
     const dynamic=img.getAttribute('data-a-dynamic-image');
     if(dynamic){try{Object.keys(JSON.parse(dynamic)).forEach(url=>add(url,name))}catch{}}
     add(img.getAttribute('data-old-hires')||img.getAttribute('data-src')||img.currentSrc||img.src,name);
   });
   document.querySelectorAll('[style*="background-image"]').forEach(node=>{const url=getComputedStyle(node).backgroundImage.match(/url\(["']?(.*?)["']?\)/)?.[1];if(url)add(url,node.getAttribute('title')||node.innerText)});
   const title=document.querySelector('#productTitle')?.innerText||document.querySelector('h1')?.innerText||document.title;
   const bullets=document.querySelector('#feature-bullets')?.innerText;
   const facts=document.querySelector('#productDetails_techSpec_section_1')?.innerText||'';
   return {version:1,source:location.href,title:title.trim(),text:(bullets?bullets+'\n'+facts:document.body.innerText).slice(0,18000),images:images.slice(0,100),notice:'来自手动浏览器采集。图片与文字是候选资料，请核对真实款式、数量和商品信息。'};
 }});
 const url=URL.createObjectURL(new Blob([JSON.stringify(result,null,2)],{type:'application/json'}));
 const a=document.createElement('a');a.href=url;a.download='product-materials-'+Date.now()+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),20000);
 status.textContent=`已导出 ${result.images.length} 张图片候选，请回工作台导入JSON。`;
}catch(error){status.textContent=error.message}
};
