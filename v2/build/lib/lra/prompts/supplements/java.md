Java 专属关注点：
- String 用 == 比较（应改用 .equals()）
- 整数除法精度丢失（应至少一个操作数强转 double）
- 对 Arrays.asList() 结果调用 add/remove/clear
- 资源未关闭（应 try-with-resources）
- 空指针风险（未判空直接解引用）
- 字符串拼接构造 SQL（应用 PreparedStatement）
