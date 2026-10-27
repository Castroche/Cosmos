async page => {
 const result=[];
 await page.setViewportSize({width:1600,height:1000});
 await page.goto('http://127.0.0.1:8010/?section=portfolio');
 await page.getByLabel('历史收益品种',{exact:true}).waitFor({timeout:30000});
 result.push({page:'portfolio',text:(await page.locator('main').innerText()).slice(0,1800)});
 await page.getByRole('button',{name:'风险控制',exact:true}).click();
 await page.getByRole('button',{name:/紧急停止开仓|恢复新开仓/}).first().waitFor({timeout:30000});
 result.push({page:'risk',text:(await page.locator('main').innerText()).slice(0,1800)});
 await page.getByRole('button',{name:'策略库',exact:true}).click();
 await page.locator('.library-table article').first().waitFor();
 await page.getByRole('button',{name:'新建个人策略',exact:true}).click();
 await page.getByLabel('策略名称',{exact:true}).fill('UI验收策略');
 await page.getByLabel('研究假设',{exact:true}).fill('验证界面创建保存和归档恢复，不参与交易');
 await page.getByRole('button',{name:'保存到个人策略库',exact:true}).click();
 await page.getByRole('button',{name:'UI验收策略',exact:false}).first().waitFor();
 await page.getByPlaceholder('搜索名称、ID 或描述').fill('UI验收策略');
 result.push({page:'library-created',text:await page.locator('.library-table').innerText()});
 return result;
}
