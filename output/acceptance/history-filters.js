async page => {
 await page.setViewportSize({width:1600,height:1000});
 await page.goto('http://127.0.0.1:8010/?section=portfolio');
 await page.getByLabel('历史收益品种').waitFor();
 await page.getByLabel('历史收益品种').selectOption('BTC/USDT:USDT');
 await page.getByLabel('历史收益时间范围').selectOption('7');
 await page.getByRole('button',{name:'刷新',exact:true}).waitFor({state:'visible'});
 await page.waitForFunction(()=>!document.querySelector('.trade-history-filters button')?.disabled);
 return {historySymbol:await page.getByLabel('历史收益品种').inputValue(),historyDays:await page.getByLabel('历史收益时间范围').inputValue(),pageError:await page.locator('[role=alert]').allTextContents()};
}
