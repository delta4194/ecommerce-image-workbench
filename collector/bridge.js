function wake(){chrome.runtime.sendMessage({type:'workbench-pump'}).catch(()=>{})}
wake();setInterval(wake,3000);
