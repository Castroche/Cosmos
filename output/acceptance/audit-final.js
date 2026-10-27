async page => {
 const issues=[];const results=[];
 page.on('pageerror',e=>issues.push({type:'pageerror',message:e.message}));
 page.on('response',r=>{if(r.status()>=400&&r.url().includes('/api/'))issues.push({type:'http',status:r.status(),url:r.url()})});
 const routes=['intelligence','portfolio','risk','researchAgent','library','editor','logs','ai','settings'];
 for(const width of [1600,768,390]){
  await page.setViewportSize({width,height:width===390?844:1000});
  for(const section of routes){
   await page.goto(`http://127.0.0.1:8010/?section=${section}`);
   await page.locator('main h1').first().waitFor({timeout:30000});
   if(section==='portfolio')await page.getByLabel('历史收益品种').waitFor({timeout:30000});
   if(section==='risk')await page.getByRole('button',{name:/紧急停止开仓|恢复新开仓/}).waitFor({timeout:30000});
   if(section==='library')await page.locator('.library-table article').first().waitFor();
   if(section==='logs')await page.locator('.audit-ledger article').first().waitFor();
   if(section==='intelligence')await page.waitForFunction(()=>[...document.querySelectorAll('.chart-indicator-legend b')].some(e=>e.textContent!=='—'),{},{timeout:30000});
   if(section==='researchAgent')await page.waitForFunction(()=>![...document.querySelectorAll('.research-status-strip strong')].some(e=>e.textContent==='读取中'),{},{timeout:30000});
   await page.screenshot({path:`output/acceptance/${section}-${width}-final.png`,fullPage:true,animations:'disabled'});
   const detail=await page.evaluate(()=>({overflow:document.documentElement.scrollWidth>innerWidth,title:document.querySelector('main h1').textContent,accent:getComputedStyle(document.documentElement).getPropertyValue('--accent').trim(),alerts:[...document.querySelectorAll('main [role=alert]')].map(e=>e.textContent)}));
   results.push({section,viewport:width,...detail});
  }
 }
 return {results,issues};
}
