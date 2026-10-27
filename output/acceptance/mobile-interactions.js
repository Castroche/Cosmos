async page => {
 await page.setViewportSize({width:390,height:844});
 await page.goto('http://127.0.0.1:8010/?section=intelligence');
 await page.getByRole('button',{name:'展开导航',exact:true}).click();
 const navOpen=await page.locator('.primary-navigation').evaluate(e=>({width:e.getBoundingClientRect().width,labelsVisible:getComputedStyle(e.querySelector('.navigation-groups button span')).display,mainWidth:document.querySelector('main').getBoundingClientRect().width}));
 await page.getByRole('button',{name:'设置',exact:true}).click();
 const collapsed=await page.locator('.primary-navigation').getAttribute('class');
 await page.getByRole('navigation',{name:'设置分类'}).getByRole('button',{name:/策略默认值/}).click();
 await page.screenshot({path:'output/acceptance/mobile-settings-tabs.png',fullPage:true,animations:'disabled'});
 await page.getByRole('button',{name:'交易工作台',exact:true}).click();
 await page.getByRole('button',{name:/指标 \d/}).click();
 const menu=await page.locator('.indicator-menu').evaluate(e=>{const r=e.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,inView:r.left>=0&&r.right<=innerWidth,hitTest:e.contains(document.elementFromPoint(Math.min(innerWidth-1,Math.max(0,r.x+r.width/2)),r.y+60))}});
 await page.screenshot({path:'output/acceptance/mobile-chart-menu.png',animations:'disabled'});
 return {navOpen,collapsed,menu};
}
