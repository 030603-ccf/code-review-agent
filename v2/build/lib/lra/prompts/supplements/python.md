Python 专属关注点：
- 可变默认参数（def f(x=[])）
- 用 `is` 比较字面量（应改用 `==`；判断 None 用 `is None` 正确）
- 未关闭的资源（文件、连接），应使用 with / try-finally
- 裸 except 吞异常
- f-string 或字符串拼接构造 SQL
- eval/exec/os.system/subprocess shell=True 的危险用法
- 循环中反复拼接字符串（应用 join）
