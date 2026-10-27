async page => {
 await page.setViewportSize({width:1600,height:1000});
 await page.getByRole('navigation',{name:'主导航',exact:true}).waitFor();
 await page.screenshot({path:'output/acceptance/desktop-before.png',fullPage:true});
 return {errors:await page.locator('[role=alert]').allTextContents(),overflow:await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth)};
}
