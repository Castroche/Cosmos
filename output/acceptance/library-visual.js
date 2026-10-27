async page => {
 await page.setViewportSize({width:1600,height:1000});
 await page.goto('http://127.0.0.1:8010/?section=library');
 await page.locator('.library-table article').first().waitFor();
 await page.screenshot({path:'output/acceptance/library-desktop-final.png',animations:'disabled'});
 await page.getByRole('button',{name:'新建个人策略',exact:true}).click();
 await page.getByRole('dialog').waitFor();
 await page.screenshot({path:'output/acceptance/strategy-dialog-final.png',animations:'disabled'});
 const rect=await page.getByRole('dialog').evaluate(e=>{let r=e.getBoundingClientRect();return {x:r.x,y:r.y,bottom:r.bottom,inView:r.left>=0&&r.right<=innerWidth&&r.top>=0&&r.bottom<=innerHeight}});
 await page.keyboard.press('Escape');
 return {modalViewport:rect};
}
