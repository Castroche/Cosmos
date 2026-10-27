async page => {
 const checks=[];
 await page.setViewportSize({width:1600,height:1000});
 await page.goto('http://127.0.0.1:8010/?section=settings');
 const nav=page.getByRole('navigation',{name:'设置分类'});
 for (const label of ['风险边界','策略默认值','交易连接']){
  await nav.getByRole('button',{name:new RegExp(label)}).click();
  checks.push({settingsTab:label,heading:await page.locator('.form-section h2').allTextContents()});
 }
 await page.getByRole('button',{name:'保存并应用',exact:true}).click();
 await page.getByRole('status').filter({hasText:'设置已保存'}).waitFor();
 checks.push('设置原值保存通过');
 await page.route('**/api/settings',route=>route.request().method()==='PUT'?route.fulfill({status:422,contentType:'application/json',body:JSON.stringify({detail:'验收：风险参数超出范围'})}):route.continue());
 await page.getByRole('button',{name:'保存并应用',exact:true}).click();
 await page.getByRole('alert').filter({hasText:'验收：风险参数超出范围'}).waitFor();
 checks.push({settingsError:await page.getByRole('alert').innerText(),class:await page.getByRole('alert').getAttribute('class')});
 await page.unroute('**/api/settings');
 await page.getByRole('button',{name:'运行日志',exact:true}).click();
 await page.locator('.audit-ledger article').first().waitFor();
 await page.getByPlaceholder('搜索事件、策略 ID 或 Agent ID').fill('不会存在的验收事件');
 await page.getByRole('button',{name:'搜索',exact:true}).click();
 await page.getByText('没有符合条件的审计事件。',{exact:true}).waitFor();
 await page.getByPlaceholder('搜索事件、策略 ID 或 Agent ID').fill('');
 await page.getByRole('button',{name:'搜索',exact:true}).click();
 await page.locator('.audit-ledger article').first().waitFor();
 const download=page.waitForEvent('download');
 await page.getByRole('button',{name:'导出当前结果'}).click();
 checks.push({logs:'搜索清空与导出通过',download:(await download).suggestedFilename()});
 await page.getByRole('button',{name:'离线研究',exact:true}).click();
 for(const label of ['候选策略','技术诊断','市场与生成']){
  await page.locator('.research-tabs').getByRole('button',{name:label,exact:true}).click();
  checks.push({researchTab:label,active:await page.locator('.research-tabs button.active').innerText()});
 }
 await page.getByRole('button',{name:'交易工作台',exact:true}).click();
 await page.locator('.ti-workbench-tabs').waitFor();
 for(const label of ['入场订单','等待策略','活动计划','学习复盘','决策记录','市场分析','实盘授权','组合持仓']){
  await page.getByRole('navigation',{name:'交易工作区'}).getByRole('button',{name:new RegExp(label)}).click();
  checks.push({terminalTab:label,active:await page.locator('.ti-workbench-tabs button.active').innerText()});
 }
 return checks;
}
