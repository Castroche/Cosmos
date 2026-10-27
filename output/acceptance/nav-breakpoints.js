async page => {
 const results=[];
 for(const width of [1280,768,390,1600]){
  await page.setViewportSize({width,height:1000});
  await page.goto('http://127.0.0.1:8010/?section=settings');
  await page.getByRole('navigation',{name:'主导航',exact:true}).waitFor();
  const open=page.getByRole('button',{name:'展开导航',exact:true});
  if(await open.count()) await open.click();
  await page.waitForFunction(()=>Math.abs(document.querySelector('nav.primary-navigation').getBoundingClientRect().width-252)<1);
  results.push(await page.locator('.primary-navigation').evaluate(e=>({viewport:innerWidth,width:e.getBoundingClientRect().width,label:getComputedStyle(e.querySelector('.navigation-groups button span')).display,mainLeft:document.querySelector('main').getBoundingClientRect().left,topbarLeft:document.querySelector('.global-topbar').getBoundingClientRect().left})));
  await page.getByRole('button',{name:'策略库',exact:true}).click();
  if(width<=1440)await page.waitForFunction(()=>document.querySelector('.primary-navigation').classList.contains('collapsed'));
 }
 return results;
}
