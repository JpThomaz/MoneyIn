classDiagram
direction BT
class account {
   varchar name
   varchar type
   char(32) user_id
   char(32) household_id
   char(32) id
}
class category {
   varchar name
   varchar type
   char(32) household_id
   char(32) id
}
class household {
   varchar name
   datetime created_at
   varchar invite_code
   char(32) id
}
class sqlite_master {
   text type
   text name
   text tbl_name
   int rootpage
   text sql
}
class transaction {
   date date
   varchar description
   float amount
   char(32) account_id
   char(32) category_id
   char(32) household_id
   boolean is_transfer
   varchar transaction_hash
   char(32) id
}
class user {
   varchar email
   varchar name
   varchar hashed_password
   char(32) household_id
   char(32) id
}

account  -->  household : household_id:id
account  -->  user : user_id:id
category  -->  household : household_id:id
transaction  -->  account : account_id:id
transaction  -->  category : category_id:id
transaction  -->  household : household_id:id
user  -->  household : household_id:id
