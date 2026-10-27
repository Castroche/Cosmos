async page => {
 const issues=[]; const results=[];
 page.on('pageerror',e=>issues.push({type:'pageerror',message:e.message}));
 page.on('response',r=>{if(r.status()>=400&&r.url().includes('/api/'))issues.push({type:'http',status:r.status(),url:r.url()})});
 const routes=['intelligence','portfolio','risk','researchAgent','library','editor','logs','ai','settings'];
 for(const width of [1600,390]){
  await page.setViewportSize({width,height:width===390?844:1000});
  for(const section of routes){
   await page.goto(`http://127.0.0.1:8010/?section=${section}`);
   await page.locator('main h1').first().waitFor({timeout:30000});
   await page.screenshot({path:`output/acceptance/${section}-${width}-before.png`,fullPage:true});
   const detail=await page.evaluate(()=>({overflow:document.documentElement.scrollWidth>innerWidth,width:document.documentElement.scrollWidth,headings:[...document.querySelectorAll('h1,h2')].map(e=>e.textContent),text:document.querySelector('main').innerText.slice(0,1200),offscreen:[...document.querySelectorAll('main button,main input,main select')].filter(e=>{let r=e.getBoundingClientRect();return r.width>0&&(r.right>innerWidth+1||r.left<0)}).map(e=>e.getAttribute('aria-label')||e.textContent).slice(0,12)}));
   results.push({section,viewport:width,...detail});
  }
 }
 return {results,issues};
}
