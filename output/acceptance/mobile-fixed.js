async page => {
 await page.setViewportSize({width:390,height:844});
 await page.goto('http://127.0.0.1:8010/?section=intelligence');
 await page.getByRole('button',{name:/指标 \d/}).click();
 await page.locator('.indicator-menu').waitFor();
 await page.screenshot({path:'output/acceptance/mobile-chart-menu-fixed.png',animations:'disabled'});
 const menu=await page.locator('.indicator-menu').evaluate(e=>{const r=e.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,bottom:r.bottom,inView:r.left>=0&&r.right<=innerWidth&&r.bottom<=innerHeight,hitTest:e.contains(document.elementFromPoint(r.x+r.width/2,r.y+60))}});
 await page.getByRole('checkbox',{name:/相对强弱/}).check();
 await page.getByRole('button',{name:'关闭指标面板'}).click();
 await page.getByRole('button',{name:'展开导航',exact:true}).click();
 await page.getByRole('button',{name:'设置',exact:true}).click();
 await page.waitForFunction(()=>document.querySelector('.primary-navigation')?.classList.contains('collapsed'));
 return {menu,navAutoClose:true};
}
