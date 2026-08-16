# C# 高频陷阱

## 空引用
- 未检查 null 直接访问成员（NullReferenceException）
- `as` 转型失败返回 null 后未判断直接使用
- 可空值类型 `int?` 直接 `.Value` 未检查 `HasValue`

## 异步
- `async void`：异常无法被调用方捕获（除事件处理外一律用 `async Task`）
- 忘记 `await`：方法静默返回未完成的 Task，异常丢失
- `.Result` / `.Wait()` 死锁：在 UI/ASP.NET 同步上下文中阻塞异步
- `ConfigureAwait(false)` 遗漏导致上下文切换开销

## 资源管理
- `IDisposable` 对象未用 `using` 包裹（文件句柄/数据库连接泄漏）
- `HttpClient` 在 using 中频繁创建（端口耗尽，应复用单例）
- 事件订阅未取消（`+=` 后无 `-=`）导致内存泄漏

## 集合与 LINQ
- `foreach` 中修改集合（InvalidOperationException）
- LINQ 延迟执行：多次枚举 = 多次查询（应 `.ToList()` 缓存）
- `Dictionary[key]` 不存在时抛异常（应用 `TryGetValue`）

## 值类型与引用类型
- struct 装箱：值类型传入 `object` 参数产生隐式装箱（性能）
- 可变 struct：方法调用在副本上操作，原值不变
- `==` 对引用类型比较地址而非内容（string 除外）
