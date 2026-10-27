async page => {
 await page.setViewportSize({width:390,height:844});
 await page.goto('http://127.0.0.1:8010/?section=risk');
 await page.getByRole('button',{name:'紧急停止开仓'}).waitFor();
 await page.screenshot({path:'output/acceptance/risk-390-final.png',fullPage:true,animations:'disabled'});
 const riskCopy=await page.locator('.risk-rule-list article > div').evaluate(e=>({width:e.clientWidth,scrollWidth:e.scrollWidth,text:e.innerText}));
 await page.goto('http://127.0.0.1:8010/?section=researchAgent');
 await page.getByText('未配置',{exact:true}).waitFor();
 return {riskCopy,researchAI:'未配置'};
}
